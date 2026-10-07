import sys

def test_twitter_scraper():
    try:
        from ntscraper import Nitter
    except ImportError:
        import subprocess
        subprocess.run([sys.executable, "-m", "pip", "install", "ntscraper"], check=True)
        from ntscraper import Nitter

    print("Testing Nitter scraper for X/Twitter...")
    scraper = Nitter(log_level=1, skip_instance_check=False)
    
    # Search for hiring project manager
    query = '#hiring "Project Manager"'
    
    print(f"Searching for: {query}")
    try:
        results = scraper.get_tweets(query, mode='term', number=5)
        
        if results and 'tweets' in results and len(results['tweets']) > 0:
            print(f"Found {len(results['tweets'])} tweets!")
            for i, tweet in enumerate(results['tweets']):
                print(f"\n{i+1}. [{tweet['date']}] {tweet['user']['name']} (@{tweet['user']['username']})")
                print(f"   {tweet['text'][:150]}...")
                print(f"   Link: {tweet['link']}")
        else:
            print("No tweets found or all Nitter instances failed.")
    except Exception as e:
        print(f"Scraping failed: {e}")

if __name__ == "__main__":
    test_twitter_scraper()
