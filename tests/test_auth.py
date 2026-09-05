import pytest


def test_health_check(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_signup_success(client):
    payload = {
        "name": "Sarah Connor",
        "email": "sarah@example.com",
        "password": "supersecurepassword123",
    }
    response = client.post("/auth/signup", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert "id" in data
    assert data["name"] == "Sarah Connor"
    assert data["email"] == "sarah@example.com"
    assert "created_at" in data
    assert "updated_at" in data
    assert "password_hash" not in data
    assert "password" not in data


def test_signup_duplicate_email(client):
    payload = {
        "name": "John Doe",
        "email": "john@example.com",
        "password": "mypassword123",
    }
    # First signup
    res1 = client.post("/auth/signup", json=payload)
    assert res1.status_code == 201

    # Second signup with same email
    res2 = client.post("/auth/signup", json=payload)
    assert res2.status_code == 400
    assert res2.json()["detail"] == "Email is already registered"


def test_signup_invalid_input(client):
    # Invalid email
    res1 = client.post(
        "/auth/signup",
        json={"name": "Test", "email": "not-an-email", "password": "password123"},
    )
    assert res1.status_code == 422

    # Short password
    res2 = client.post(
        "/auth/signup",
        json={"name": "Test", "email": "test@example.com", "password": "123"},
    )
    assert res2.status_code == 422


def test_login_success(client):
    # Create user first
    signup_payload = {
        "name": "Alex Murphy",
        "email": "alex@example.com",
        "password": "robocop_password",
    }
    res_signup = client.post("/auth/signup", json=signup_payload)
    assert res_signup.status_code == 201

    # Login
    login_payload = {
        "email": "alex@example.com",
        "password": "robocop_password",
    }
    res_login = client.post("/auth/login", json=login_payload)
    assert res_login.status_code == 200
    token_data = res_login.json()
    assert "access_token" in token_data
    assert token_data["token_type"] == "bearer"
    assert len(token_data["access_token"]) > 20


def test_login_invalid_password(client):
    signup_payload = {
        "name": "Alex Murphy",
        "email": "alex_wrong@example.com",
        "password": "correct_password",
    }
    client.post("/auth/signup", json=signup_payload)

    # Wrong password
    res = client.post(
        "/auth/login",
        json={"email": "alex_wrong@example.com", "password": "wrong_password"},
    )
    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid email or password"


def test_login_nonexistent_user(client):
    res = client.post(
        "/auth/login",
        json={"email": "ghost@example.com", "password": "some_password"},
    )
    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid email or password"


def test_auth_me_success(client):
    signup_payload = {
        "name": "Ellen Ripley",
        "email": "ripley@weyland.com",
        "password": "nostromo_password_1979",
    }
    res_signup = client.post("/auth/signup", json=signup_payload)
    assert res_signup.status_code == 201

    login_res = client.post(
        "/auth/login",
        json={"email": "ripley@weyland.com", "password": "nostromo_password_1979"},
    )
    assert login_res.status_code == 200
    token = login_res.json()["access_token"]

    # Call /auth/me with Bearer token
    headers = {"Authorization": f"Bearer {token}"}
    me_res = client.get("/auth/me", headers=headers)
    assert me_res.status_code == 200
    me_data = me_res.json()
    assert me_data["name"] == "Ellen Ripley"
    assert me_data["email"] == "ripley@weyland.com"
    assert "id" in me_data
    assert "password_hash" not in me_data


def test_auth_me_missing_token(client):
    res = client.get("/auth/me")
    assert res.status_code == 401
    assert res.json()["detail"] == "Authentication token is required"


def test_auth_me_invalid_token(client):
    headers = {"Authorization": "Bearer invalid.fake.token"}
    res = client.get("/auth/me", headers=headers)
    assert res.status_code == 401
    assert res.json()["detail"] == "Could not validate credentials"
