"""
role_config.py — Defines job categories and regex matching patterns.
This configuration is used by pipeline.py to categorize massive job dumps from `all_*` scrapers.
"""
import re

JOB_ROLES = [
    # --- ENGINEERING & TECH ---
    {
        "name": "Software Engineer",
        "slug": "software_engineer",
        "title_patterns": [
            r"\bsoftware\b", r"\bbackend\b", r"\bfrontend\b", r"\bfull[\s\-/]*stack\b",
            r"\bsde\b", r"\bdeveloper\b", r"\bprogrammer\b", r"\bweb\b", r"\bmts\b",
            r"\bengineer\b" # Broadest fallback for engineering
        ],
        "fallback_patterns": [
            r"\bsoftware[\s\-/]+engineer", r"\bsoftware[\s\-/]+developer", r"\bfull\s*stack",
            r"\bfrontend[\s\-/]+engineer", r"\bbackend[\s\-/]+engineer",
            r"\bweb[\s\-/]+developer", r"\bplatform[\s\-/]+engineer"
        ]
    },
    {
        "name": "Mobile Engineer",
        "slug": "mobile_engineer",
        "title_patterns": [
            r"\bios\b", r"\bandroid\b", r"\bmobile\b", r"\bflutter\b", r"\breact[\s\-/]*native\b"
        ],
        "fallback_patterns": [
            r"\bmobile[\s\-/]+(?:developer|engineer)", r"\b(?:ios|android)[\s\-/]+(?:developer|engineer)"
        ]
    },
    {
        "name": "Hardware / Embedded",
        "slug": "hardware_engineer",
        "title_patterns": [
            r"\bhardware\b", r"\bembedded\b", r"\bfirmware\b", r"\belectrical\b", r"\bmechanical\b"
        ],
        "fallback_patterns": [
            r"\bhardware[\s\-/]+engineer", r"\bembedded[\s\-/]+software"
        ]
    },
    {
        "name": "AI / ML Engineer",
        "slug": "ai_ml_engineer",
        "title_patterns": [
            r"\bai\b", r"\bml\b", r"\bmachine[\s\-/]*learning\b", r"\bdata[\s\-/]*scient\w+",
            r"\bllm\w*", r"\bgenerative\b", r"\bgenai\b", r"\bdeep[\s\-/]*learning\b",
            r"\bcomputer[\s\-/]*vision\b", r"\bnlp\b", r"\bprompt\b"
        ],
        "fallback_patterns": [
            r"\b(?:artificial[\s\-/]+intelligence|ai)[\s\-/]+engineer", r"\bmachine[\s\-/]+learning",
            r"\bdata[\s\-/]+scientist"
        ]
    },
    {
        "name": "Data Engineer",
        "slug": "data_engineer",
        "title_patterns": [
            r"\bdata[\s\-/]*engineer\w*", r"\betl\b", r"\bdata[\s\-/]*infrastructure\b",
            r"\bbig[\s\-/]*data\b", r"\bdata[\s\-/]*platform\b"
        ],
        "fallback_patterns": [
            r"\bdata[\s\-/]+engineer", r"\betl[\s\-/]+engineer"
        ]
    },
    {
        "name": "Data Analyst / BI",
        "slug": "data_analyst",
        "title_patterns": [
            r"\bdata[\s\-/]*analy\w+", r"\bbi\b", r"\bbusiness[\s\-/]*intelligence\b",
            r"\banalyst\b", r"\bdata\b"
        ],
        "fallback_patterns": [
            r"\bdata[\s\-/]+analyst", r"\bbusiness[\s\-/]+intelligence"
        ]
    },
    {
        "name": "DevOps / SRE",
        "slug": "devops_sre",
        "title_patterns": [
            r"\bdevops\b", r"\bsre\b", r"\breliability\b", r"\binfrastructure\b",
            r"\bcloud\b", r"\bplatform\b", r"\bkubernetes\b", r"\baws\b", r"\bazure\b"
        ],
        "fallback_patterns": [
            r"\bdevops", r"\bsite[\s\-/]+reliability"
        ]
    },
    {
        "name": "Cybersecurity",
        "slug": "cybersecurity",
        "title_patterns": [
            r"\bsecurity\b", r"\bcybersecurity\b", r"\binfosec\b", r"\bthreat\b",
            r"\bappsec\b", r"\bpenetration\b"
        ],
        "fallback_patterns": [
            r"\bsecurity[\s\-/]+engineer", r"\bcybersecurity"
        ]
    },
    {
        "name": "QA / Test Engineer",
        "slug": "qa_test",
        "title_patterns": [
            r"\bqa\b", r"\bquality\b", r"\btest\w*\b", r"\bsdet\b", r"\bautomation\b"
        ],
        "fallback_patterns": [
            r"\bqa[\s\-/]+engineer", r"\bquality[\s\-/]+assurance"
        ]
    },
    {
        "name": "Product Manager",
        "slug": "product_manager",
        "title_patterns": [
            r"\bproduct\b", r"\bpm\b", r"\bowner\b"
        ],
        "fallback_patterns": [
            r"\bproduct[\s\-/]+manager", r"\bpm\b"
        ]
    },
    {
        "name": "UX/UI Designer",
        "slug": "ux_ui_designer",
        "title_patterns": [
            r"\bux\b", r"\bui\b", r"\bdesign\w*\b", r"\buser[\s\-/]*experience\b"
        ],
        "fallback_patterns": [
            r"\bux[\s\-/]+designer", r"\bui[\s\-/]+designer", r"\bproduct[\s\-/]+designer"
        ]
    },
    {
        "name": "Sales / Account Executive",
        "slug": "sales",
        "title_patterns": [
            r"\bsales\b", r"\baccount\b", r"\bbdr\b", r"\bsdr\b", r"\bclient\b", r"\brepresentative\b"
        ],
        "fallback_patterns": [
            r"\baccount[\s\-/]+executive", r"\bsales[\s\-/]+manager"
        ]
    },
    {
        "name": "Marketing",
        "slug": "marketing",
        "title_patterns": [
            r"\bmarketing\b", r"\bgrowth\b", r"\bseo\b", r"\bcontent\b", r"\bsocial\b", r"\bevent\b"
        ],
        "fallback_patterns": [
            r"\bmarketing[\s\-/]+manager", r"\bgrowth[\s\-/]+marketing"
        ]
    },
    {
        "name": "Human Resources / Recruiting",
        "slug": "hr_recruiting",
        "title_patterns": [
            r"\bhr\b", r"\brecruit\w*\b", r"\btalent\b", r"\bsourcing\b", r"\bpeople\b", r"\bhuman[\s\-/]*resources\b"
        ],
        "fallback_patterns": [
            r"\bhuman[\s\-/]+resources", r"\bhr\b", r"\brecruiter"
        ]
    },
    {
        "name": "Finance / Accounting",
        "slug": "finance",
        "title_patterns": [
            r"\bfinanc\w*\b", r"\baccountant\b", r"\baccounting\b", r"\bpayroll\b", r"\btax\b", r"\bcontroller\b"
        ],
        "fallback_patterns": [
            r"\bfinance\b", r"\bfinancial[\s\-/]+analyst"
        ]
    },
    {
        "name": "Legal / Compliance",
        "slug": "legal",
        "title_patterns": [
            r"\blegal\b", r"\bcounsel\b", r"\bparalegal\b", r"\battorney\b", r"\bcompliance\b", r"\blawyer\b"
        ],
        "fallback_patterns": [
            r"\blegal[\s\-/]+counsel", r"\bgeneral[\s\-/]+counsel"
        ]
    },
    {
        "name": "Operations / Logistics",
        "slug": "operations",
        "title_patterns": [
            r"\boperations\b", r"\bsupply\b", r"\blogistics\b", r"\bfacilit\w+\b", r"\bwarehouse\b", r"\bstrategy\b"
        ],
        "fallback_patterns": [
            r"\boperations[\s\-/]+manager", r"\bsupply[\s\-/]+chain"
        ]
    },
    {
        "name": "Administrative",
        "slug": "administrative",
        "title_patterns": [
            r"\badministrat\w+\b", r"\bassistant\b", r"\breceptionist\b"
        ],
        "fallback_patterns": [
            r"\bexecutive[\s\-/]+assistant", r"\breceptionist"
        ]
    },
    {
        "name": "Executive / Management",
        "slug": "executive",
        "title_patterns": [
            r"\bchief\b", r"\bceo\b", r"\bcto\b", r"\bcfo\b", r"\bcoo\b", r"\bcmo\b", r"\bvp\b", r"\bvice[\s\-/]*president\b",
            r"\bdirector\b", r"\bhead\b", r"\bpresident\b", r"\bmanager\b"
        ],
        "fallback_patterns": [
            r"\bchief[\s\-/]+\w+[\s\-/]+officer", r"\bceo\b"
        ]
    }
]

# Note: Order matters. More specific roles (e.g. AI/ML Engineer) must appear before broader ones (e.g. Software Engineer).
# Wait, I need to make sure AI Engineer is before Software Engineer in the list.
# I will do that dynamically.
JOB_ROLES.insert(0, JOB_ROLES.pop(3)) # Move AI/ML up
JOB_ROLES.insert(1, JOB_ROLES.pop(4)) # Move Data Engineer up
JOB_ROLES.insert(2, JOB_ROLES.pop(5)) # Move Data Analyst up
JOB_ROLES.insert(3, JOB_ROLES.pop(6)) # Move DevOps up
JOB_ROLES.insert(4, JOB_ROLES.pop(7)) # Move Cybersecurity up
JOB_ROLES.insert(5, JOB_ROLES.pop(8)) # Move QA up
JOB_ROLES.insert(6, JOB_ROLES.pop(2)) # Move Hardware up
JOB_ROLES.insert(7, JOB_ROLES.pop(1)) # Move Mobile up

DROP_UNMATCHED = False

def classify_role(title: str, description: str = "", skills: str = "") -> dict | None:
    """
    Takes a job title and returns the matching role dict, 
    or None if no match is found. Uses a 2-pass system.
    Pass 1 matches broad keywords (e.g., 'backend', 'data') on the TITLE ONLY.
    This guarantees high recall ("showing results which are around it") without the
    false positives that would occur if we searched the description.
    """
    title_lower = title.lower()
    
    priority_patterns = [
        (
            {
                "name": "Internship / Early Career",
                "slug": "internship_early_career",
            },
            [
                r"\bintern\b", r"\binternship\b", r"\bnew[\s\-/]+grad",
                r"\bearly[\s\-/]+career", r"\btrainee\b", r"\bapprentice"
            ],
            [
                r"\bintern\b", r"\binternship\b"
            ]
        )
    ]
    
    # PASS 1: Broad Keyword Matching on the Title
    for role, title_pats, fallback_pats in priority_patterns:
        for pattern in title_pats:
            if re.search(pattern, title_lower):
                return role

    for role in JOB_ROLES:
        for pattern in role["title_patterns"]:
            if re.search(pattern, title_lower):
                return role
                
    # PASS 2: Fallback to Strict Patterns on Skills & Description
    fallback_text = f"{title} {skills} {description[:500]}".lower()
    
    for role, title_pats, fallback_pats in priority_patterns:
        for pattern in fallback_pats:
            if re.search(pattern, fallback_text):
                return role

    for role in JOB_ROLES:
        for pattern in role["fallback_patterns"]:
            if re.search(pattern, fallback_text):
                return role
                
    return None
