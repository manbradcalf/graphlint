from graphlint.parser import parse_schema
from graphlint.backends.cypher import CypherBackend
from graphlint.runner import dry_run

# SHACL example
with open("examples/movies.shacl.ttl") as f:
    schema = f.read()

plan = parse_schema(schema, source="movies.shacl.ttl")
print(dry_run(plan, CypherBackend()))
