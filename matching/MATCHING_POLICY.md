# Matching policy v2

This policy combines the existing compute-once hybrid engine with the useful
eligibility and preference rules from the supplied matching-algorithm document.

## Invariant

```text
GLOBAL JOB INTELLIGENCE
  -> candidate eligibility filters
  -> bounded semantic + lexical retrieval
  -> reciprocal-rank fusion
  -> candidate-specific evidence scoring
  -> High / Medium / Low recommendations
```

No resume, candidate, subscription, or request identifier participates in job
artifact extraction. Salary, posting date, work mode, employment type and role
remain globally reusable attributes of a job.

## Eligibility before similarity

Explicit strict choices are enforced before ANN or lexical ranking:

| Rule | Strict behavior |
|---|---|
| Target role | Only canonical selected role subfamilies are eligible |
| Location | Only selected places are eligible; remote jobs remain location-independent |
| Work mode | Remote-only cannot return hybrid, onsite, or unknown modes |
| Employment type | Only selected types are eligible |
| Experience | Jobs whose stated minimum exceeds professional experience are excluded |
| Salary | When non-flexible, a compatible known annual salary must meet the minimum |
| Freshness | Jobs older than the selected age are excluded; unknown dates remain explicit unknowns |
| Blocked companies | Exact normalized company matches are excluded |

Hard rules are never relaxed to meet a subscription quota. A larger requested
count is filled with Medium and then Low matches only from the eligible set.

## Ranking evidence

The score combines required skills, preferred skills/tools, semantic evidence,
role, professional experience, location/work mode, education, employment
preference, salary and freshness. Missing fields are excluded from the weighted
denominator rather than treated as confirmed failures.

Five safeguards prevent misleading High matches:

1. A desired title is separate from roles evidenced by professional experience.
   Target intent without career evidence receives partial role credit only.
2. Less than half of stated required skills caps the result below High.
3. A severe experience shortfall caps the result below High.
4. A title two or more career levels away from the candidate (seniority fit
   below 0.5) caps the result below High.
5. Thin evidence shrinks the score toward 0.5. When the known structured
   features (everything except semantic similarity, which is derived from the
   same text and always present) carry less than 60% of their total weight, the
   distance from 0.5 is scaled by the share present. A posting known only by its
   title can reach Medium but not High, and never outranks an equally relevant,
   well-described posting.

Seniority is read from the title on one scale for jobs and candidates (intern,
junior, mid, senior, lead, director, executive). Management is its own track
between senior and lead; a title without a level word counts as mid-level.
When the taxonomy cannot classify a job's role, title similarity to the
candidate's stated roles stands in for it instead of the role feature being
dropped, which previously exempted unclassified jobs from any role penalty.

Role aliases are canonicalized. Software Engineering is treated as an umbrella
for backend, frontend and full-stack engineering, but unrelated technology roles
and product-management roles are not treated as equivalent.

## Confidence labels

- High: score at least 75 and no evidence safeguard has capped the result.
- Medium: score from 50 through 74.99.
- Low: score below 50.

The numeric value is a deterministic match score, not a calibrated probability
of receiving an interview. Calling it an interview probability would require a
labeled outcome model and calibration data.

## Result page

- The same title at the same company is shown once (the best-scored copy),
  whichever board or city it was scraped from.
- At most three results per company per page; further results from that
  company only fill slots nothing else eligible could fill.

## Deliberate exclusions

- Jobs are ordered by relevance, not by job board. Board grouping belongs in the
  presentation layer and must not override relevance.
- Fortune 500 status is not scored until a versioned, trustworthy company dataset
  is available and the product explicitly asks the user whether this is preferred.
- No per-job LLM call is used online. Extracted global artifacts and deterministic
  explanations preserve latency, reuse, auditability and cost bounds.
