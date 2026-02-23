import pytest


@pytest.fixture
def examples_dir(request):
    return request.config.rootpath / "examples"


@pytest.fixture
def movies_shex(examples_dir):
    return (examples_dir / "movies.shex").read_text()


@pytest.fixture
def movies_shacl(examples_dir):
    return (examples_dir / "movies.shacl.ttl").read_text()
