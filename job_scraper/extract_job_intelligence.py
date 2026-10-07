"""
extract_job_intelligence.py
===========================
Extracts high-value intelligence from job descriptions:
  1. reports_to       — Who this role reports to (e.g., "VP of Engineering")
  2. works_with_cxo   — Does the role work directly with C-Suite? (Yes/No/Not Mentioned)
  3. cxo_titles_found — Which specific CXO titles appear (e.g., "CEO, CTO")
  4. team_size        — Any mention of team size managed (e.g., "team of 5")
  5. key_stakeholders — Other senior stakeholders mentioned alongside the role

Runs on: job_scraper/indeed_jobs/ULTRA_STRICT_49_Roles_Indeed.xlsx
Output:  job_scraper/indeed_jobs/ULTRA_STRICT_49_Roles_Indeed_ENRICHED.xlsx
"""

import re
import pandas as pd
from typing import Optional

# ─── REGEX PATTERNS ──────────────────────────────────────────────────────────

# "reports to", "reporting to", "will report to", "reports directly to", etc.
REPORTS_TO_PATTERNS = [
    r'(?:this\s+(?:role|position)\s+)?(?:will\s+)?report(?:ing|s)?\s+(?:directly\s+)?to[\s:,]+([A-Z][^\n.!?,;]{3,60})',
    r'(?:directly\s+)?report(?:ing|s)?\s+to\s+the\s+([A-Z][^\n.!?,;]{3,60})',
    r'position\s+reports?\s+to[\s:]+([A-Z][^\n.!?,;]{3,60})',
    r'you\s+will\s+report\s+to[\s:]+([A-Z][^\n.!?,;]{3,60})',
]
REPORTS_TO_RE = re.compile('|'.join(REPORTS_TO_PATTERNS), re.IGNORECASE)

# CXO titles — ordered longest-first to avoid partial matches
CXO_TITLES = [
    'Chief Executive Officer', 'Chief Technology Officer', 'Chief Financial Officer',
    'Chief Operating Officer', 'Chief Marketing Officer', 'Chief Product Officer',
    'Chief Revenue Officer', 'Chief Information Officer', 'Chief People Officer',
    'Chief Human Resources Officer', 'Chief Security Officer', 'Chief Data Officer',
    'Chief Customer Officer', 'Chief Legal Officer',
    'CEO', 'CTO', 'CFO', 'COO', 'CMO', 'CPO', 'CRO', 'CIO', 'CISO', 'CHRO', 'CDO',
    'Co-Founder', 'Cofounder', 'Founder',
    'President', 'Vice President', 'VP',
    'SVP', 'EVP', 'Managing Director', 'Executive Director',
    'C-Suite', 'C-Level', 'C Suite', 'CXO',
    'Board of Directors', 'Board Member',
]
CXO_RE = re.compile(
    r'\b(' + '|'.join(re.escape(t) for t in CXO_TITLES) + r')\b',
    re.IGNORECASE,
)

# "work directly with", "partner with", "collaborate with", "interact with"
INTERACTION_TRIGGERS = re.compile(
    r'(?:work(?:ing)?\s+(?:directly\s+)?with|partner(?:ing)?\s+with|'
    r'collaborat(?:e|ing)\s+with|interact(?:ing)?\s+with|'
    r'interface\s+with|liaise\s+with|cross[- ]?functional)',
    re.IGNORECASE,
)

# Team size patterns — broader lookahead to match "manages X+ employees", "leading 5 engineers"
TEAM_SIZE_RE = re.compile(
    r'(?:manag(?:e|es|ing)|lead(?:s|ing)|oversee(?:s|ing)?|supervis(?:e|es|ing)?|coordinating)\s+'
    r'(?:a\s+(?:cross[- ]?functional\s+)?team\s+of\s+)?(\d+[\+\-]?(?:\s*(?:to|-|–)\s*\d+)?)'
    r'\s*(?:person|people|member|engineer|developer|report|staff|employee|contractor)',
    re.IGNORECASE,
)


# ─── EXTRACTION FUNCTIONS ─────────────────────────────────────────────────────

# Job title words — captured string must contain at least one of these to be valid
ROLE_WORDS_RE = re.compile(
    r'\b(?:manager|director|president|officer|lead|head|chief|vp|vice\s+president|'
    r'supervisor|executive|owner|partner|founder|ceo|cto|cfo|coo|cmo|cpo|'
    r'principal|coordinator|engineer|analyst|architect|team|board)\b',
    re.IGNORECASE,
)

def extract_reports_to(desc: str) -> Optional[str]:
    """Extract who the role reports to."""
    if not desc or not isinstance(desc, str):
        return None
    
    match = REPORTS_TO_RE.search(desc)
    if not match:
        return None
    
    # Pick the first non-None capturing group
    captured = next((g for g in match.groups() if g), None)
    if not captured:
        return None
    
    # Clean up: strip markdown, bullets, extra spaces
    captured = re.sub(r'\*+', '', captured).strip()
    captured = re.sub(r'\s+', ' ', captured)
    
    # Trim at first sentence-like boundary words to avoid run-on captures
    captured = re.split(r'\s+(?:and|or|while|who|with|as|to|for|in|on)\s+', captured, maxsplit=1)[0]
    
    # Trim at punctuation that wasn't caught by main regex
    captured = re.split(r'[,;:|]', captured)[0].strip()
    
    # Max 65 chars
    if len(captured) > 65:
        captured = captured[:65].rsplit(' ', 1)[0]
    
    # Validate: must look like a job title (contain a role keyword)
    if not ROLE_WORDS_RE.search(captured):
        return None
    
    return captured.strip() if len(captured) > 3 else None


def extract_cxo_info(desc: str) -> tuple[str, str]:
    """
    Returns:
        works_with_cxo  : 'Yes' | 'No' | 'Not Mentioned'
        cxo_titles_found: comma-separated unique CXO titles found near triggers
    """
    if not desc or not isinstance(desc, str):
        return 'Not Mentioned', ''
    
    # Split into sentences for context-aware matching
    sentences = re.split(r'[.\n!?]', desc)
    
    direct_cxo_titles = set()
    
    for sentence in sentences:
        cxo_matches = CXO_RE.findall(sentence)
        if not cxo_matches:
            continue
        
        has_trigger = bool(INTERACTION_TRIGGERS.search(sentence))
        reports_sentence = bool(re.search(r'reports?\s+to|reporting\s+to', sentence, re.IGNORECASE))
        
        if has_trigger or reports_sentence:
            for title in cxo_matches:
                # Normalize common abbreviations
                normalized = {
                    'vp': 'VP', 'svp': 'SVP', 'evp': 'EVP',
                    'ceo': 'CEO', 'cto': 'CTO', 'cfo': 'CFO',
                    'coo': 'COO', 'cmo': 'CMO', 'cpo': 'CPO',
                    'cro': 'CRO', 'cio': 'CIO', 'ciso': 'CISO',
                    'chro': 'CHRO', 'cdo': 'CDO', 'cxo': 'CXO',
                }.get(title.lower(), title)
                direct_cxo_titles.add(normalized)
    
    if direct_cxo_titles:
        return 'Yes', ', '.join(sorted(direct_cxo_titles))
    
    # Check if CXO titles appear anywhere in the description (indirect mention)
    all_cxo = CXO_RE.findall(desc)
    if all_cxo:
        return 'No', ''  # Mentions CXO but NOT as someone you'll work with
    
    return 'Not Mentioned', ''


def extract_team_size(desc: str) -> Optional[str]:
    """Extract team size or number of direct reports."""
    if not desc or not isinstance(desc, str):
        return None
    match = TEAM_SIZE_RE.search(desc)
    if not match:
        return None
    size = next((g for g in match.groups() if g), None)
    return size.strip() if size else None


def extract_key_stakeholders(desc: str) -> Optional[str]:
    """Extract senior stakeholders mentioned in collaboration context."""
    if not desc or not isinstance(desc, str):
        return None
    
    # Look for seniority + interaction patterns
    STAKEHOLDER_RE = re.compile(
        r'(?:work(?:ing)?\s+(?:closely\s+)?with|partner(?:ing)?\s+with|'
        r'collaborat(?:e|ing)\s+(?:closely\s+)?with|alongside)'
        r'\s+([A-Z][^.\n!?,;]{3,60})',
        re.IGNORECASE,
    )
    
    matches = STAKEHOLDER_RE.findall(desc)
    if not matches:
        return None
    
    # Filter: keep only those with seniority indicators
    SENIORITY_KEYWORDS = re.compile(
        r'\b(director|manager|lead|head|senior|principal|vp|vice\s+president|'
        r'chief|president|exec|owner|founder|board|stakeholder)\b',
        re.IGNORECASE,
    )
    senior_matches = [m.strip() for m in matches if SENIORITY_KEYWORDS.search(m)]
    
    if not senior_matches:
        return None
    
    # Deduplicate and trim
    seen = set()
    result = []
    for s in senior_matches:
        key = s.lower()[:30]
        if key not in seen:
            seen.add(key)
            result.append(s[:60])
    
    return ' | '.join(result[:3]) if result else None


# ─── MAIN PIPELINE ────────────────────────────────────────────────────────────

def enrich_excel(input_path: str, output_path: str):
    print(f"\n📂 Loading: {input_path}")
    df = pd.read_excel(input_path)
    print(f"   Shape: {df.shape}")
    
    desc_col = 'description'
    if desc_col not in df.columns:
        print(f"❌ No '{desc_col}' column found. Columns: {df.columns.tolist()}")
        return
    
    descs = df[desc_col].fillna('').astype(str)
    
    print("🔍 Extracting intelligence from descriptions...")
    
    df['reports_to']        = descs.apply(extract_reports_to)
    df['works_with_cxo']    = descs.apply(lambda d: extract_cxo_info(d)[0])
    df['cxo_titles_found']  = descs.apply(lambda d: extract_cxo_info(d)[1])
    df['team_size_managed'] = descs.apply(extract_team_size)
    df['key_stakeholders']  = descs.apply(extract_key_stakeholders)
    
    # Summary stats
    total = len(df)
    has_reports_to    = df['reports_to'].notna().sum()
    works_with_cxo_y  = (df['works_with_cxo'] == 'Yes').sum()
    has_team_size     = df['team_size_managed'].notna().sum()
    has_stakeholders  = df['key_stakeholders'].notna().sum()
    
    print(f"\n📊 Extraction Results:")
    print(f"   Total rows           : {total}")
    print(f"   'reports_to' found   : {has_reports_to} ({has_reports_to/total*100:.1f}%)")
    print(f"   'works_with_cxo=Yes' : {works_with_cxo_y} ({works_with_cxo_y/total*100:.1f}%)")
    print(f"   'team_size_managed'  : {has_team_size} ({has_team_size/total*100:.1f}%)")
    print(f"   'key_stakeholders'   : {has_stakeholders} ({has_stakeholders/total*100:.1f}%)")
    
    # Sample outputs
    print("\n🔎 Sample 'reports_to' extractions:")
    samples = df[df['reports_to'].notna()][['title', 'company', 'reports_to']].head(8)
    for _, row in samples.iterrows():
        print(f"   [{row['title']} @ {row['company']}]  →  {row['reports_to']}")
    
    print("\n🔎 Sample 'works_with_cxo=Yes':")
    cxo_samples = df[df['works_with_cxo'] == 'Yes'][['title', 'company', 'cxo_titles_found']].head(5)
    for _, row in cxo_samples.iterrows():
        print(f"   [{row['title']} @ {row['company']}]  →  {row['cxo_titles_found']}")
    
    # Reorder columns — put new intel columns right after company
    base_cols = ['id', 'site', 'job_url', 'title', 'company', 'location', 'date_posted',
                 'Searched_Role']
    new_cols  = ['reports_to', 'works_with_cxo', 'cxo_titles_found',
                 'team_size_managed', 'key_stakeholders']
    rest_cols = [c for c in df.columns if c not in base_cols + new_cols]
    
    final_order = [c for c in base_cols if c in df.columns] + new_cols + rest_cols
    df = df[final_order]
    
    # Clean for Excel
    for col in df.select_dtypes(include=['object']).columns:
        df[col] = df[col].astype(str).replace('nan', '').replace('None', '')
    
    df.to_excel(output_path, index=False)
    print(f"\n💾 Saved enriched file to: {output_path}")
    print("✅ Done!\n")


if __name__ == '__main__':
    enrich_excel(
        input_path  = 'job_scraper/indeed_jobs/ULTRA_STRICT_49_Roles_Indeed.xlsx',
        output_path = 'job_scraper/indeed_jobs/ULTRA_STRICT_49_Roles_Indeed_ENRICHED.xlsx',
    )
