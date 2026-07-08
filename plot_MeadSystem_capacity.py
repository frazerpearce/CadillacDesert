#!/usr/bin/env python3
"""
Plot unwrapped live storage for Lake Powell, Flaming Gorge and Lake Mead.

Curves:
  - Coloured: each reservoir as percentage of its own live capacity.
  - Black: combined storage as percentage of combined live capacity.
  - Dotted: minimum-power-pool reference levels over the final third of the plot.

Data source:
  Bureau of Reclamation Hydrodata reservoir dashboards.

Output:
  reservoir_live_storage_percent_unwrapped.png

Requirements:
  python -m pip install pandas matplotlib

Examples:
  python plot_reservoir_live_storage_percent_clean.py
  python plot_reservoir_live_storage_percent_clean.py --start-year 2021 --end-year 2025
  python plot_reservoir_live_storage_percent_clean.py --years 30 --output storage.png
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from dataclasses import dataclass
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Any

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd


BASE_URL = "https://www.usbr.gov/uc/water/hydrodata/reservoir_data"
STORAGE_ITEM_ID = "17"

RESERVOIRS = {
    "Powell": "919",
    "Flaming Gorge": "917",
    "Mead": "921",
}

LIVE_CAPACITY_AF = {
    "Powell": 23_310_000.0,
    "Flaming Gorge": 3_749_000.0,
    "Mead": 26_120_000.0,
}
TOTAL_LIVE_CAPACITY_AF = sum(LIVE_CAPACITY_AF.values())

# Minimum-power-pool live storage, acre-feet.
MIN_POWER_STORAGE_AF = {
    "Powell": 4_126_000.0,
    "Flaming Gorge": 233_000.0,
    "Mead": 4_550_000.0,
}
MIN_POWER_PERCENT = {
    name: 100.0 * storage / LIVE_CAPACITY_AF[name]
    for name, storage in MIN_POWER_STORAGE_AF.items()
}
TOTAL_MIN_POWER_PERCENT = (
    100.0 * sum(MIN_POWER_STORAGE_AF.values()) / TOTAL_LIVE_CAPACITY_AF
)

COMPONENT_COLOURS = {
    "Mead": "tab:blue",
    "Powell": "tab:orange",
    "Flaming Gorge": "tab:green",
}


@dataclass(frozen=True)
class ReservoirSeries:
    name: str
    storage_af: pd.Series


def fetch_bytes(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 reservoir-storage-plot/1.0",
            "Accept": "application/json,text/csv,*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as response:
        return response.read()


def to_numeric_series(s: pd.Series) -> pd.Series:
    return pd.to_numeric(
        s.astype(str).str.replace(",", "", regex=False).str.strip(),
        errors="coerce",
    )


def date_score(s: pd.Series) -> tuple[float, pd.Series]:
    if s.dropna().empty:
        return 0.0, pd.Series(pd.NaT, index=s.index)

    parsed = pd.to_datetime(s, errors="coerce", utc=False)
    score = parsed.notna().mean()

    numeric = pd.to_numeric(s, errors="coerce")
    if numeric.notna().mean() > score:
        med = numeric.dropna().median()
        unit = "ms" if med > 1.0e11 else "s"
        parsed_epoch = pd.to_datetime(numeric, unit=unit, errors="coerce", utc=False)
        epoch_score = parsed_epoch.notna().mean()
        if epoch_score > score:
            return epoch_score, parsed_epoch

    return score, parsed


def select_date_and_value_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        raise ValueError("Empty data frame")

    df = df.dropna(axis=1, how="all").copy()
    df.columns = [str(c) for c in df.columns]

    date_col, parsed_dates = max(
        (
            (
                score + (0.25 if "date" in col.lower() or "time" in col.lower() else 0.0),
                col,
                parsed,
            )
            for col in df.columns
            for score, parsed in [date_score(df[col])]
        ),
        key=lambda item: item[0],
    )[1:]

    if parsed_dates.notna().mean() < 0.5:
        raise ValueError(f"Could not identify a date column. Columns: {list(df.columns)}")

    value_candidates: list[tuple[float, str, pd.Series]] = []
    for col in df.columns:
        if col == date_col:
            continue

        values = to_numeric_series(df[col])
        valid = values.notna().mean()
        if valid < 0.5:
            continue

        name = col.lower()
        priority = valid
        priority += 0.5 if "storage" in name else 0.0
        priority += 0.25 if "value" in name or name == "val" else 0.0
        priority += 0.25 if "acre" in name or "af" in name else 0.0
        priority -= 0.5 if any(bad in name for bad in ("flag", "qual", "code", "id")) else 0.0
        value_candidates.append((priority, col, values))

    if not value_candidates:
        raise ValueError(f"Could not identify a numeric storage column. Columns: {list(df.columns)}")

    _, _, values = max(value_candidates, key=lambda item: item[0])

    out = pd.DataFrame(
        {
            "date": pd.to_datetime(parsed_dates).dt.tz_localize(None).dt.normalize(),
            "storage_af": values,
        }
    )
    out = out.dropna(subset=["date", "storage_af"])
    out = out.sort_values("date").drop_duplicates("date", keep="last")
    out = out.set_index("date")

    if out.empty:
        raise ValueError("No valid date/storage rows after parsing")

    return out


def flatten_json_to_dataframes(obj: Any) -> list[pd.DataFrame]:
    frames: list[pd.DataFrame] = []

    def visit(x: Any) -> None:
        if isinstance(x, list):
            if not x:
                return

            if all(isinstance(row, dict) for row in x):
                frames.append(pd.DataFrame(x))
            elif all(isinstance(row, (list, tuple)) for row in x):
                max_len = max(len(row) for row in x)
                if max_len >= 2:
                    rows = [list(row) + [None] * (max_len - len(row)) for row in x]
                    frames.append(pd.DataFrame(rows))

            for item in x:
                visit(item)

        elif isinstance(x, dict):
            list_keys = [k for k, v in x.items() if isinstance(v, list)]
            if len(list_keys) >= 2 and len({len(x[k]) for k in list_keys}) == 1:
                try:
                    frames.append(pd.DataFrame({k: x[k] for k in list_keys}))
                except Exception:
                    pass

            for item in x.values():
                visit(item)

    visit(obj)
    return frames


def parse_json_storage(payload: bytes, url: str) -> pd.DataFrame:
    obj = json.loads(payload.decode("utf-8"))
    errors: list[str] = []

    for frame in flatten_json_to_dataframes(obj):
        try:
            parsed = select_date_and_value_columns(frame)
            if len(parsed) > 100:
                return parsed
        except Exception as exc:
            errors.append(str(exc))

    raise ValueError(f"Could not parse JSON storage time series from {url}. Errors: {errors[:5]}")


def parse_csv_storage(payload: bytes, url: str) -> pd.DataFrame:
    errors: list[str] = []

    for skiprows in range(20):
        try:
            parsed = select_date_and_value_columns(
                pd.read_csv(BytesIO(payload), skiprows=skiprows)
            )
            if len(parsed) > 100:
                return parsed
        except Exception as exc:
            errors.append(str(exc))

    raise ValueError(f"Could not parse CSV storage time series from {url}. Errors: {errors[:5]}")


def read_storage(reservoir_name: str, reservoir_id: str) -> ReservoirSeries:
    json_url = f"{BASE_URL}/{reservoir_id}/json/{STORAGE_ITEM_ID}.json"
    try:
        df = parse_json_storage(fetch_bytes(json_url), json_url)
    except Exception as json_exc:
        print(
            f"JSON failed for {reservoir_name}; trying CSV. Reason: {json_exc}",
            file=sys.stderr,
        )
        csv_url = f"{BASE_URL}/{reservoir_id}/csv/{STORAGE_ITEM_ID}.csv"
        df = parse_csv_storage(fetch_bytes(csv_url), csv_url)

    return ReservoirSeries(reservoir_name, df["storage_af"].clip(lower=0.0))


def build_merged_table(series: list[ReservoirSeries]) -> pd.DataFrame:
    reservoir_cols = list(RESERVOIRS)
    df = pd.concat({item.name: item.storage_af for item in series}, axis=1).sort_index()
    df = df.interpolate(method="time", limit=7, limit_direction="both")

    df["Total"] = df[reservoir_cols].sum(axis=1, min_count=len(reservoir_cols))
    df = df.dropna(subset=["Total"])

    for col in reservoir_cols:
        df[f"{col}_pct"] = 100.0 * df[col] / LIVE_CAPACITY_AF[col]
    df["Total_pct"] = 100.0 * df["Total"] / TOTAL_LIVE_CAPACITY_AF

    return df


def add_min_power_lines(ax: plt.Axes, start: pd.Timestamp, plot_end: pd.Timestamp) -> None:
    x0 = start + (plot_end - start) * 2 / 3

    for name, colour in COMPONENT_COLOURS.items():
        pct = MIN_POWER_PERCENT[name]
        ax.plot(
            [x0, plot_end],
            [pct, pct],
            color=colour,
            linestyle=":",
            linewidth=1.4,
            alpha=0.9,
            zorder=1,
            label=f"{name}: min. power pool ({pct:.1f}%)",
        )

    ax.plot(
        [x0, plot_end],
        [TOTAL_MIN_POWER_PERCENT, TOTAL_MIN_POWER_PERCENT],
        color="black",
        linestyle=":",
        linewidth=2.2,
        alpha=0.95,
        zorder=1,
        label=f"Combined min. power pool ({TOTAL_MIN_POWER_PERCENT:.1f}%)",
    )


def add_capacity_note(ax: plt.Axes) -> None:
    capacity_text = (
        "Live capacities\n"
        "Powell 23.310 MAF\n"
        "Flaming Gorge 3.749 MAF\n"
        "Mead 26.120 MAF\n"
        f"Combined {TOTAL_LIVE_CAPACITY_AF / 1.0e6:.3f} MAF"
    )
    ax.text(
        0.50,
        0.02,
        capacity_text,
        transform=ax.transAxes,
        ha="center",
        va="bottom",
        fontsize=9,
        alpha=0.75,
    )


def plot_unwrapped(df: pd.DataFrame, years: list[int], output: Path) -> None:
    start = pd.Timestamp(year=years[0], month=1, day=1)
    end = pd.Timestamp(year=years[-1], month=12, day=31)
    plot_df = df.loc[(df.index >= start) & (df.index <= end)].copy()

    if plot_df.empty:
        raise ValueError(f"No data available for selected date range: {start.date()}-{end.date()}")

    fig, ax = plt.subplots(figsize=(12, 7))

    for name, colour in COMPONENT_COLOURS.items():
        ax.plot(
            plot_df.index,
            plot_df[f"{name}_pct"],
            color=colour,
            linewidth=1.5,
            alpha=0.75,
            zorder=2,
            label=f"{name}: % full",
        )

    ax.plot(
        plot_df.index,
        plot_df["Total_pct"],
        color="black",
        linewidth=3.2,
        alpha=0.95,
        zorder=5,
        label="Combined total: % of combined live capacity",
    )

    plot_end = min(end, plot_df.index.max())
    add_min_power_lines(ax, start, plot_end)

    ax.set_ylabel("Percentage capacity")
    ax.set_xlabel("Date")
    ax.set_ylim(0, 100)
    ax.set_xlim(start, plot_end)
    ax.grid(True, alpha=0.3)

    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%y"))
    ax.xaxis.set_minor_locator(mdates.MonthLocator(interval=3))

    ax.legend(
        loc="lower left",
        bbox_to_anchor=(0.01, 0.02),
        frameon=False,
        title="Series / reference levels",
        borderaxespad=0.0,
        fontsize=8,
    )
    add_capacity_note(ax)

    fig.tight_layout()
    fig.savefig(output, dpi=200)
    print(f"Wrote {output}")


def parse_args() -> argparse.Namespace:
    current_year = date.today().year

    parser = argparse.ArgumentParser(
        description="Plot unwrapped live-storage percentage for Powell, Flaming Gorge and Mead."
    )
    parser.add_argument("--start-year", type=int, default=2021, help="First calendar year to plot.")
    parser.add_argument("--end-year", type=int, default=current_year, help="Last calendar year to plot.")
    parser.add_argument(
        "--years",
        type=int,
        default=None,
        help="Plot this many years ending at --end-year. Overrides --start-year.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reservoir_live_storage_percent_unwrapped.png"),
        help="Output PNG filename.",
    )
    args = parser.parse_args()

    if args.years is not None:
        if args.years < 1:
            parser.error("--years must be >= 1")
        args.start_year = args.end_year - args.years + 1

    if args.start_year > args.end_year:
        parser.error("--start-year must be <= --end-year")

    return args


def main() -> None:
    args = parse_args()

    downloaded = [read_storage(name, reservoir_id) for name, reservoir_id in RESERVOIRS.items()]
    df = build_merged_table(downloaded)

    data_start_year = int(df.index.year.min())
    data_end_year = int(df.index.year.max())

    if args.end_year > data_end_year:
        print(
            f"Requested end year {args.end_year}, but latest downloaded data year is "
            f"{data_end_year}; plotting available data only.",
            file=sys.stderr,
        )
    if args.start_year < data_start_year:
        print(
            f"Requested start year {args.start_year}, but downloaded data begins in "
            f"{data_start_year}; plotting available data only.",
            file=sys.stderr,
        )

    first_year = max(args.start_year, data_start_year)
    last_year = min(args.end_year, data_end_year)
    years = list(range(first_year, last_year + 1))

    if not years:
        raise ValueError(
            f"Requested year range {args.start_year}-{args.end_year} does not overlap "
            f"available data range {data_start_year}-{data_end_year}."
        )

    print(f"Plotting calendar years {years[0]}-{years[-1]}")
    plot_unwrapped(df, years, args.output)


if __name__ == "__main__":
    main()
