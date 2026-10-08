"""
scrape_procurement_hyper_grid.py
======================================
A hyper-refined slicing grid to completely bypass LinkedIn's 130-job guest limit.
Slices by Top 20 US Tech Hubs / Major Cities x 3 Time Windows = 60 micro-searches.
"""

import sys
import os
import time
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

sys.path.append(os.path.abspath('/Users/AminBhavya/Ai_Sales_Agent/JobSpy-main'))
from jobspy import scrape_jobs

MAX_CONCURRENT = 10
semaphore = threading.Semaphore(MAX_CONCURRENT)

# ─── PARALLEL SCRAPER WORKER ──────────────────────────────────────────────────
def scrape_task(args):
    loc, search_term, hours_old, window_label = args
    with semaphore:
        label = f"{loc} [{window_label}]"
        print(f"  🟡 {label}")
        try:
            jobs = scrape_jobs(
                site_name=["linkedin"],
                search_term=search_term,
                location=loc,
                results_wanted=1000,
                hours_old=hours_old,
                linkedin_fetch_description=False,
            )
            print(f"  ✅ {label}: {len(jobs)} jobs")
            if not jobs.empty:
                jobs['search_location'] = loc
                jobs['time_window'] = window_label
            return jobs
        except Exception as e:
            print(f"  ❌ {label} failed: {e}")
            return pd.DataFrame()

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    search_term = "procurement"
    
    # HYPER-REFINED LOCATION SLICING (Top 20 Major Hubs instead of whole states)
    locations = [
        "San Francisco, CA", "Los Angeles, CA", "San Diego, CA", "San Jose, CA",
        "New York, NY", "Brooklyn, NY", "Buffalo, NY",
        "Austin, TX", "Dallas, TX", "Houston, TX", "San Antonio, TX",
        "Seattle, WA", "Boston, MA", "Chicago, IL",
        "Atlanta, GA", "Miami, FL", "Denver, CO", "Washington, DC",
        "Philadelphia, PA", "Phoenix, AZ"
    ]

    # REFINED TIME SLICING
    time_windows = [
        (24,  "Last 24 Hours"),
        (72,  "Last 3 Days"),
        (168, "Last 7 Days")
    ]

    total_tasks = len(locations) * len(time_windows)
    print(f"🚀 Hyper-Grid LinkedIn Scrape — '{search_term}'")
    print(f"   {len(locations)} cities × {len(time_windows)} time windows = {total_tasks} micro-searches")
    print(f"   Concurrency: {MAX_CONCURRENT} simultaneous threads\n")

    task_args = [(loc, search_term, h, lbl) for loc in locations for h, lbl in time_windows]
    all_dfs = []

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT) as executor:
        futures = [executor.submit(scrape_task, args) for args in task_args]
        for future in as_completed(futures):
            df = future.result()
            if df is not None and not df.empty:
                all_dfs.append(df)

    if not all_dfs:
        print("⚠️ No data collected.")
        return

    merged = pd.concat(all_dfs, ignore_index=True)
    raw_total = len(merged)
    merged = merged.drop_duplicates(subset=['job_url'])
    
    print(f"\n📦 Grid Complete! Collected {raw_total} raw jobs.")
    print(f"  Unique jobs: {len(merged)} (dropped {raw_total - len(merged)} duplicates)")

    # Clean text for Excel
    for col in merged.select_dtypes(include=['object']).columns:
        merged[col] = merged[col].astype(str).replace({'nan':'','None':''})
        merged[col] = merged[col].apply(lambda x: ''.join(ch for ch in x if ord(ch)>31 or ord(ch) in [9,10,13]) if isinstance(x,str) else x)

    output = "job_scraper/linkedin_jobs/Procurement_HyperGrid_LinkedIn.xlsx"
    merged.to_excel(output, index=False)
    print(f"\n💾 Saved → {output}")

if __name__ == '__main__':
    main()
