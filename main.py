from graphlint.parser import parse_schema
from graphlint.backends.cypher import CypherBackend
from graphlint.runner import dry_run

# ShExC example (also supports SHACL — try examples/movies.shacl.ttl)
with open("examples/movies.shex") as f:
    schema = f.read()

plan = parse_schema(schema, source="movies.shex")
print(dry_run(plan, CypherBackend()))
