"""
Export the cloud data from Firestore to CSV and measure how real-time it is.

Usage:
    python export.py --key serviceAccount.json --out export [--hours 24]

Writes
    export/gps_fixes.csv         own GPS data (same columns as assignment 2 + cloud fields)
    export/bikes_snapshots.csv   dublinbikes open data, one row per station per snapshot
    export/realtime_latency.png  upload latency of GPS fixes + bikes feed freshness
and prints a real-time summary. gps_fixes.csv can be fed to analyse.py as before.
"""

import argparse
import os
import time

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

GPS_COLUMNS = ["deviceId", "timestamp", "latitude", "longitude", "accuracy",
               "altitude", "speed", "heading", "sessionId", "seq", "receivedAt", "latency_s"]


def ms(ts):
    """Firestore timestamp (datetime) -> epoch milliseconds."""
    return None if ts is None else ts.timestamp() * 1000


def gps_frame(docs):
    rows = []
    for d in docs:
        rows.append({
            "deviceId":   d.get("deviceId"),
            "timestamp":  pd.to_datetime(d["timestamp"], unit="ms", utc=True),
            "latitude":   d.get("latitude"),
            "longitude":  d.get("longitude"),
            "accuracy":   d.get("accuracy"),
            "altitude":   d.get("altitude"),
            "speed":      d.get("speed"),
            "heading":    d.get("heading"),
            "sessionId":  d.get("sessionId"),
            "seq":        d.get("seq"),
            "receivedAt": pd.to_datetime(ms(d.get("receivedAt")), unit="ms", utc=True),
            # time from the phone taking the fix to Firestore committing it
            "latency_s":  (ms(d["receivedAt"]) - d["timestamp"]) / 1000 if d.get("receivedAt") else np.nan,
        })
    df = pd.DataFrame(rows, columns=GPS_COLUMNS)
    return df.sort_values("timestamp").reset_index(drop=True)


def bikes_frame(snapshots, stations):
    rows = []
    for s in snapshots:
        for sid, st in s["stations"].items():
            info = stations.get(sid, {})
            rows.append({
                "snapshot_id":       s.get("snapshotId") or s["_id"],
                "gbfs_last_updated": s["gbfsLastUpdated"],
                "fetched_at":        s["fetchedAt"],
                "received_at":       s.get("receivedAt"),
                "station_id":        sid,
                "name":              info.get("name"),
                "lat":               info.get("lat"),
                "lon":               info.get("lon"),
                "capacity":          info.get("capacity"),
                "bikes":             st.get("bikes"),
                "docks":             st.get("docks"),
                "bikes_disabled":    st.get("bikesDisabled"),
                "docks_disabled":    st.get("docksDisabled"),
                "renting":           st.get("renting"),
                "last_reported":     pd.to_datetime(st.get("lastReported"), unit="s", utc=True),
            })
    return pd.DataFrame(rows)


def snapshot_frame(snapshots):
    df = pd.DataFrame([{
        "snapshot_id":       s["_id"],
        "gbfs_last_updated": pd.Timestamp(s["gbfsLastUpdated"]),
        "fetched_at":        pd.Timestamp(s["fetchedAt"]),
        "received_at":       pd.Timestamp(s["receivedAt"]) if s.get("receivedAt") else pd.NaT,
        "bikes":             s["totals"]["bikes"],
        "docks":             s["totals"]["docks"],
    } for s in snapshots])
    if df.empty:
        return df
    df = df.sort_values("fetched_at").reset_index(drop=True)
    df["feed_age_s"] = (df["fetched_at"] - df["gbfs_last_updated"]).dt.total_seconds()
    df["write_delay_s"] = (df["received_at"] - df["fetched_at"]).dt.total_seconds()
    df["interval_s"] = df["fetched_at"].diff().dt.total_seconds()
    return df


def summarise(gps, snaps):
    print("=" * 60)
    print("REAL-TIME SUMMARY")
    print("=" * 60)
    if gps.empty:
        print("GPS: no fixes in range")
    else:
        lat = gps["latency_s"].dropna()
        print(f"GPS fixes:         {len(gps)} from {gps['deviceId'].nunique()} device(s), "
              f"{gps['sessionId'].nunique()} session(s)")
        print(f"  time range:      {gps['timestamp'].min()} -> {gps['timestamp'].max()}")
        print(f"  upload latency:  median {lat.median():.2f} s, 95th pct {lat.quantile(0.95):.2f} s, "
              f"max {lat.max():.2f} s")
        print(f"  under 2 s:       {(lat < 2).mean():.1%}")
        missing = 0
        for _, g in gps.groupby("sessionId"):
            seq = g["seq"].dropna().astype(int)
            missing += (seq.max() + 1 - seq.nunique()) if len(seq) else 0
        print(f"  missing seq nos: {missing} (fixes taken on the phone but not in the cloud)")
    if snaps.empty:
        print("Bikes: no snapshots in range")
    else:
        print(f"Bike snapshots:    {len(snaps)}  ({snaps['fetched_at'].min()} -> {snaps['fetched_at'].max()})")
        print(f"  poll interval:   median {snaps['interval_s'].median():.0f} s, "
              f"max {snaps['interval_s'].max():.0f} s")
        print(f"  feed age at poll: median {snaps['feed_age_s'].median():.0f} s")
        print(f"  write delay:     median {snaps['write_delay_s'].median():.2f} s")
        print(f"  latest totals:   {snaps['bikes'].iloc[-1]} bikes, {snaps['docks'].iloc[-1]} free docks")
    print("=" * 60)


def plot(gps, snaps, out):
    fig, axes = plt.subplots(1, 2, figsize=(12, 3.8))
    ax = axes[0]
    if not gps.empty:
        lat = gps["latency_s"].dropna()
        ax.hist(lat.clip(upper=lat.quantile(0.99)), bins=40)
        ax.axvline(lat.median(), color="red", ls="--", lw=1, label=f"median {lat.median():.2f} s")
        ax.legend()
    ax.set_xlabel("Fix taken on phone -> committed in Firestore (s)")
    ax.set_ylabel("Fixes")
    ax.set_title("Own data: upload latency per GPS fix")
    ax = axes[1]
    if not snaps.empty:
        ax.plot(snaps["fetched_at"], snaps["bikes"], lw=1.2, label="bikes available")
        ax.plot(snaps["fetched_at"], snaps["docks"], lw=1.2, label="free docks")
        ax.legend()
        fig.autofmt_xdate()
    ax.set_ylabel("Count across all stations")
    ax.set_title("Open data: dublinbikes snapshots in Firestore")
    path = os.path.join(out, "realtime_latency.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"saved {path}")


def fetch(key, hours):
    import firebase_admin
    from firebase_admin import credentials, firestore
    from google.cloud.firestore_v1.base_query import FieldFilter

    firebase_admin.initialize_app(credentials.Certificate(key))
    db = firestore.client()
    since_ms = int((time.time() - hours * 3600) * 1000)
    gps = [d.to_dict() for d in db.collection("gps_fixes")
           .where(filter=FieldFilter("timestamp", ">=", since_ms)).stream()]
    since = pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=hours)
    snaps = []
    for d in db.collection("bikes_snapshots").where(
            filter=FieldFilter("fetchedAt", ">=", since.to_pydatetime())).stream():
        s = d.to_dict()
        s["_id"] = d.id
        snaps.append(s)
    info = db.collection("bike_stations").document("info").get()
    stations = (info.to_dict() or {}).get("stations", {}) if info.exists else {}
    return gps, snaps, stations


def main():
    parser = argparse.ArgumentParser(description="Export Firestore data and check real-time behaviour")
    parser.add_argument("--key", required=True, help="Firebase service-account JSON key")
    parser.add_argument("--out", default="export", help="output folder")
    parser.add_argument("--hours", type=float, default=24, help="how far back to export")
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    gps_docs, snap_docs, stations = fetch(args.key, args.hours)
    gps = gps_frame(gps_docs)
    snaps = snapshot_frame(snap_docs)
    gps.to_csv(os.path.join(args.out, "gps_fixes.csv"), index=False)
    bikes_frame(snap_docs, stations).to_csv(os.path.join(args.out, "bikes_snapshots.csv"), index=False)
    print(f"saved {args.out}/gps_fixes.csv ({len(gps)} rows), {args.out}/bikes_snapshots.csv")
    summarise(gps, snaps)
    plot(gps, snaps, args.out)


if __name__ == "__main__":
    main()
