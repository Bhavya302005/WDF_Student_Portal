#!/usr/bin/env python3
import argparse
import csv
import logging
import sys
from pathlib import Path

# Local imports from the jobspy library
from jobspy import scrape_jobs
from jobspy.model import Site

def setup_logger(verbose: bool) -> logging.Logger:
    """Configures a professional-grade logger for the script execution."""
    logger = logging.getLogger("ScraperExecution")
    level = logging.DEBUG if verbose else logging.INFO
    logger.setLevel(level)
    
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(level)
        formatter = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        
    return logger

def parse_arguments() -> argparse.Namespace:
    """Parses command line arguments to allow dynamic scraping."""
    parser = argparse.ArgumentParser(description="Professional Job Scraper (LinkedIn & Indeed)")
    
    parser.add_argument("--role", type=str, default="Project Manager", help="The job role to search for.")
    parser.add_argument("--location", type=str, default="USA", help="The location to search in.")
    parser.add_argument("--country", type=str, default="USA", help="The country domain to use (e.g. USA).")
    parser.add_argument("--hours-old", type=int, default=168, help="How many hours old the jobs can be (168 = 1 week).")
    parser.add_argument("--results", type=int, default=30, help="Number of results to retrieve per site.")
    parser.add_argument("--site", type=str, nargs="+", default=["linkedin", "indeed"], help="Job boards to scrape (linkedin, indeed, etc)")
    parser.add_argument("--proxies", type=str, nargs="+", help="List of proxies to use (e.g. http://proxy.com:8080)")
    parser.add_argument("--output", type=Path, default=Path("project_manager_usa.csv"), help="Output CSV/JSON file path.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging.")
    
    return parser.parse_args()

def main():
    args = parse_arguments()
    logger = setup_logger(args.verbose)
    
    logger.info("Initializing scraping job...")
    logger.info(f"Target Role: '{args.role}' | Sites: {args.site} | Location: '{args.location}' | Age limit: {args.hours_old} hrs")
    
    try:
        # Execute the scraping logic using sites that don't block this IP easily
        
        # Map string site names to Enum
        site_enums = [Site(s.lower()) for s in args.site]
        
        jobs_df = scrape_jobs(
            site_name=site_enums,
            search_term=args.role,
            location=args.location,
            country_indeed=args.country,
            hours_old=args.hours_old,
            results_wanted=args.results,
            proxies=args.proxies,
            linkedin_fetch_description=True,
        )
        
        if jobs_df is None or jobs_df.empty:
            logger.warning("No jobs were found matching your criteria.")
            return

        logger.info(f"Successfully scraped {len(jobs_df)} jobs.")
        
        # Display a quick preview (top 5 rows) for validation
        preview_columns = ["title", "company", "location"]
        if "date_posted" in jobs_df.columns:
            preview_columns.append("date_posted")
            
        logger.info("\nPreview of top results:")
        print(jobs_df[preview_columns].head().to_string(index=False))
        
        # Save securely to the correct format
        output_path = args.output.resolve()
        logger.info(f"Saving results to {output_path}...")
        
        if output_path.suffix.lower() == ".json":
            json_str = jobs_df.to_json(orient="records", indent=4, force_ascii=False)
            json_str = json_str.replace("\\/", "/")
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(json_str)
        else:
            jobs_df.to_csv(
                output_path, 
                quoting=csv.QUOTE_NONNUMERIC, 
                escapechar="\\", 
                index=False
            )
        
        logger.info("Scraping execution completed successfully.")
        
    except Exception as e:
        logger.error(f"A critical error occurred during scraping: {e}", exc_info=args.verbose)
        sys.exit(1)

if __name__ == "__main__":
    main()
