def test_health(client):
    res = client.get('/health')
    assert res.status_code == 200
    body = res.json()
    assert body['status'] == 'ok'
    assert isinstance(body['providers'], list)
