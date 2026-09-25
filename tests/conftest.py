import pytest

import app as app_module
from service.container import build_services


@pytest.fixture
def svc():
    services = build_services()
    app_module.services = services
    return services


@pytest.fixture
def client(svc):
    app_module.app.testing = True
    with app_module.app.test_client() as c:
        yield c


def jpost(client, path, payload):
    return client.post(path, json=payload)
