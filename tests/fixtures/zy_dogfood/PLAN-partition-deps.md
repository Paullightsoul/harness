# PLAN — fixture for partition / Depends DAG regression

| id | название | depends_on | status |
|----|----------|------------|--------|
| 001 | checklist | — | todo |
| 002 | evidence-a | 001 | todo |
| 003 | evidence-b | 001 | todo |
| 004 | evidence-c | 002, 003 | todo |
| 005 | evidence-d | 001 | todo |
| 006 | phpunit | 001 | todo |
| 007 | synthesis | 004, 005, 006 | todo |

## Partition (must not overwrite DAG)

| id | wave | writers |
|----|------|---------|
| 001 | A | checklist |
| 002 | B | a |
| 003 | B | b |
| 004 | C | c |
| 005 | B | d |
| 006 | B | php |
| 007 | C | synth |
