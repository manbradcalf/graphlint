from graphlint.parser import parse_shexc_to_plan
from graphlint.backends.cypher import CypherBackend
from graphlint.runner import dry_run

with open("examples/movies.shex") as f:
    shexc = f.read()

plan = parse_shexc_to_plan(shexc, source="movies.shex")
print(dry_run(plan, CypherBackend()))
