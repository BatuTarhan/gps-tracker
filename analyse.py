"""
Usage:
    python analyse.py gps.csv --out figs

Prints a summary of data-quality issues.
"""

import argparse
import os

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

EARTH_RADIUS_M = 6_371_000

# Thresholds used to flag problems
POOR_ACCURACY_M = 20       # fixes with a larger uncertainty radius are "poor"
GAP_S = 5                  # time between fixes above this counts as a gap
NEAR_DUPLICATE_S = 0.1     # two fixes closer than this in time are near-duplicates
MAX_WALK_SPEED_MPS = 3.0   # implied speeds above this are physically implausible on foot


def haversine(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres between arrays of coordinates."""
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = (np.sin((lat2 - lat1) / 2) ** 2
         + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_RADIUS_M * np.arcsin(np.sqrt(a))


def load(path):
    df = pd.read_csv(path, parse_dates=["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    # Derived columns: time since start, time step, distance step, implied speed
    df["t_s"] = (df["timestamp"] - df["timestamp"].iloc[0]).dt.total_seconds()
    df["dt_s"] = df["t_s"].diff()
    df["dist_m"] = haversine(df["latitude"].shift(), df["longitude"].shift(),
                             df["latitude"], df["longitude"])
    # Skip implied speed for near-duplicate timestamps (dividing by ~0 explodes)
    valid = df["dt_s"] > NEAR_DUPLICATE_S
    df["implied_speed"] = np.where(valid, df["dist_m"] / df["dt_s"], np.nan)
    return df


def summarise(df):
    n = len(df)
    duration = df["t_s"].iloc[-1]
    gaps = df[df["dt_s"] > GAP_S]
    near_dups = df[df["dt_s"] < NEAR_DUPLICATE_S]
    same_pos = df.duplicated(["latitude", "longitude"]).sum()
    poor = df[df["accuracy"] > POOR_ACCURACY_M]
    fast = df[df["implied_speed"] > MAX_WALK_SPEED_MPS]
    both = df.dropna(subset=["speed", "implied_speed"])
    speed_diff = (both["speed"] - both["implied_speed"]).abs()

    print("=" * 60)
    print("GPS DATA QUALITY SUMMARY")
    print("=" * 60)
    print(f"Points:            {n}")
    print(f"Devices:           {df['deviceId'].nunique()}")
    print(f"Duration:          {duration / 60:.1f} min "
          f"({df['timestamp'].iloc[0]} -> {df['timestamp'].iloc[-1]})")
    print(f"Distance (raw):    {df['dist_m'].sum():.0f} m")
    print(f"Median interval:   {df['dt_s'].median():.3f} s")
    print()
    print("Imperfection - uncertainty")
    print(f"  accuracy radius: median {df['accuracy'].median():.1f} m, "
          f"min {df['accuracy'].min():.1f} m, max {df['accuracy'].max():.1f} m")
    print(f"  fixes > {POOR_ACCURACY_M} m: {len(poor)} ({len(poor) / n:.1%})")
    print()
    print("Imperfection - incompleteness")
    print(f"  gaps > {GAP_S} s:     {len(gaps)} "
          f"(longest {df['dt_s'].max():.1f} s)")
    for _, row in gaps.iterrows():
        print(f"    {row['timestamp']}  gap of {row['dt_s']:.1f} s")
    for col in ["speed", "heading", "altitude"]:
        missing = df[col].isna().sum()
        print(f"  missing {col:<9} {missing} ({missing / n:.1%})")
    print()
    print("Imperfection - granularity")
    print(f"  ~1 Hz sampling; {(df['dt_s'].between(0.95, 1.05)).mean():.1%} "
          f"of intervals are 1 s +/- 0.05")
    print()
    print("Inconsistency - outliers")
    print(f"  implied speed > {MAX_WALK_SPEED_MPS} m/s: {len(fast)} points "
          f"(max {df['implied_speed'].max():.1f} m/s)")
    print()
    print("Inconsistency - disorder / redundancy")
    print(f"  timestamps in order: {df['timestamp'].is_monotonic_increasing}")
    print(f"  near-duplicate fixes (< {NEAR_DUPLICATE_S} s apart): {len(near_dups)}")
    print(f"  repeated identical positions: {same_pos}")
    print()
    print("Inconsistency - conflict")
    print(f"  |reported speed - implied speed|: median {speed_diff.median():.2f} m/s, "
          f"95th pct {speed_diff.quantile(0.95):.2f} m/s")
    print("=" * 60)


def save(fig, out, name):
    path = os.path.join(out, name)
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"saved {path}")


def plot(df, out):
    os.makedirs(out, exist_ok=True)
    minutes = df["t_s"] / 60

    # 1. Track coloured by accuracy (uncertainty), with outliers marked
    fig, ax = plt.subplots(figsize=(7, 6))
    ax.plot(df["longitude"], df["latitude"], color="lightgrey", lw=0.8, zorder=1)
    sc = ax.scatter(df["longitude"], df["latitude"], c=df["accuracy"],
                    cmap="viridis", s=8, zorder=2)
    fast = df["implied_speed"] > MAX_WALK_SPEED_MPS
    ax.scatter(df.loc[fast, "longitude"], df.loc[fast, "latitude"],
               facecolors="none", edgecolors="red", s=60, zorder=3,
               label=f"implied speed > {MAX_WALK_SPEED_MPS} m/s")
    fig.colorbar(sc, ax=ax, label="Accuracy radius (m)")
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_title("GPS track coloured by reported accuracy")
    ax.set_aspect(1 / np.cos(np.radians(df["latitude"].mean())))  # undistort
    if fast.any():
        ax.legend(loc="best")
    save(fig, out, "1_track_accuracy.png")

    # 2. Accuracy over time
    fig, ax = plt.subplots(figsize=(9, 3.5))
    ax.plot(minutes, df["accuracy"], lw=1)
    ax.axhline(POOR_ACCURACY_M, color="red", ls="--", lw=1,
               label=f"{POOR_ACCURACY_M} m threshold")
    ax.set_xlabel("Time since start (min)")
    ax.set_ylabel("Accuracy radius (m)")
    ax.set_title("Positional uncertainty over time")
    ax.legend()
    save(fig, out, "2_accuracy_over_time.png")

    # 3. Time between consecutive fixes (log scale shows gaps and near-duplicates)
    fig, ax = plt.subplots(figsize=(9, 3.5))
    ax.scatter(minutes, df["dt_s"], s=6)
    ax.axhline(GAP_S, color="red", ls="--", lw=1, label=f"gap threshold ({GAP_S} s)")
    ax.set_yscale("log")
    ax.set_xlabel("Time since start (min)")
    ax.set_ylabel("Interval (s, log)")
    ax.set_title("Sampling interval between consecutive fixes")
    ax.legend()
    save(fig, out, "3_sampling_interval.png")

    # 4. Reported vs implied speed (conflict between two estimates)
    fig, ax = plt.subplots(figsize=(9, 3.5))
    ax.plot(minutes, df["implied_speed"], lw=0.8, alpha=0.7,
            label="Implied (distance / time)")
    ax.plot(minutes, df["speed"], lw=1.2, label="Reported by phone")
    ax.set_xlabel("Time since start (min)")
    ax.set_ylabel("Speed (m/s)")
    ax.set_title("Reported vs implied speed")
    ax.legend()
    save(fig, out, "4_speed_conflict.png")

    # 5. Altitude over time
    fig, ax = plt.subplots(figsize=(9, 3.5))
    ax.plot(minutes, df["altitude"], lw=1)
    ax.set_xlabel("Time since start (min)")
    ax.set_ylabel("Altitude (m)")
    ax.set_title("Reported altitude over time")
    save(fig, out, "5_altitude.png")


def main():
    parser = argparse.ArgumentParser(description="Analyse GPS tracker CSV")
    parser.add_argument("csv", help="path to gps.csv exported from the web app")
    parser.add_argument("--out", default="figures", help="output folder for figures")
    args = parser.parse_args()

    df = load(args.csv)
    summarise(df)
    plot(df, args.out)


if __name__ == "__main__":
    main()
