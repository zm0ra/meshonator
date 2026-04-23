from meshonator.auth.security import bootstrap_admin


def test_login_and_dashboard_access(client, db):
    bootstrap_admin(db, "admin", "admin", "admin")
    r = client.post('/login', data={"username": "admin", "password": "admin"}, follow_redirects=False)
    assert r.status_code in (302, 303)
    cookies = r.cookies
    home = client.get('/', cookies=cookies)
    assert home.status_code == 200
