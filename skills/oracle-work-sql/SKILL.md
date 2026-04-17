---
name: oracle-work-sql
description: Generate Oracle SQL and related work scripts in the user's established business style based on an accumulated corpus of real job SQL. Use when the user asks for Oracle SQL, telecom-style customer-list extraction, churn/port-out/contract tracking, upsell or outbound-task logic, B2I analysis, broadband or convergence marketing, or says to write SQL according to their historical scripts, existing rules, or previous task style. Prefer this skill for natural-language-to-SQL requests grounded in the user's ongoing Oracle work corpus.
---

# Oracle Work SQL

Use this skill to answer natural-language work requests by reusing the user's established Oracle SQL style, business rules, and task patterns.

## What this skill is for

Treat the user's SQL corpus as a continuously updated working knowledge base, not a one-off sample.

Default goals:
- write Oracle SQL in the user's familiar style
- reuse historical business rules when the request clearly matches a known task pattern
- produce practical outputs, not abstract pseudo-SQL
- prefer list-generation, tracking, and intermediate-table workflows when they fit the task

## What to read

Read only what is relevant:

- `references/business-domains.md` for task classification
- `references/high-frequency-tables.md` for likely source tables
- `references/rule-patterns.md` for business-rule habits
- `references/sql-style.md` for Oracle syntax and output style
- `references/query-templates.md` for choosing a generation pattern

## Working method

1. Classify the request into a business domain.
2. Infer whether the user needs:
   - a detailed list
   - a summary metric query
   - a multi-month tracking query
   - a staged script with temporary tables
3. Reuse the user's Oracle style:
   - `to_char`, `to_date`, `add_months`, `substr`
   - `nvl`, `decode`, `case when`
   - analytic functions and partitioning where appropriate
4. Prefer historically common source tables instead of inventing new data sources.
5. Preserve business filters that usually matter for this task type.
6. If a critical rule is likely month-specific or recently changed, say what needs confirmation.

## Output rules

### If the request is simple
Provide one Oracle SQL statement plus short notes on assumptions.

### If the request resembles a real production task
Prefer one of these:
- stepwise SQL with temporary/intermediate tables
- `create table as select` workflow
- SQL plus a small helper script if the user explicitly wants file export/processing

### For customer-list / outreach tasks
Do not return only `user_id` unless the user asks for a minimal result. Prefer business-usable output columns such as:
- device number
- user id
- product / dinner info
- innet date or innet months
- contract fields
- current status
- churn / cancellation / recharge / fee fields
- recommended strategy tags when relevant

## Important defaults

- Default to Oracle SQL, not another dialect.
- Default to the user's historical business style, not generic textbook SQL.
- Treat comments in historical SQL as meaningful business knowledge.
- Ignore incidental Python utility files unless the user asks for export or file processing.

## When to be careful

Call out uncertainty when:
- the request depends on a rule that often changes month to month
- the exact output table or latest rule version is unclear
- a field name is not confidently supported by the learned corpus

In those cases, still provide the closest usable draft and mark the assumptions.

## Incremental learning

When the user gives new SQL bundles later, update the knowledge base by:
- identifying the business domain
- adding new tables or rules only when they are genuinely new
- revising old rule summaries when newer scripts clearly supersede them
