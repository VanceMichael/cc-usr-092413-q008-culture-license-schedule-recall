from app import app
def test_health():
    with app.test_client() as client:
        assert client.get('/healthz').get_json() == {'status':'ok'}
