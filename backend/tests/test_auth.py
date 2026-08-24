"""验证密码哈希与 JWT 签发/校验。"""

from backend.auth import create_access_token, decode_access_token, hash_password, verify_password


def test_password_hash_verification() -> None:
    encoded = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", encoded)
    assert not verify_password("wrong password", encoded)


def test_access_token_rejects_wrong_secret() -> None:
    token = create_access_token({"sub": "user-1"}, "secret", 60)
    claims = decode_access_token(token, "secret")
    assert claims is not None
    assert claims["sub"] == "user-1"
    assert isinstance(claims["exp"], int)
    assert decode_access_token(token, "other-secret") is None
