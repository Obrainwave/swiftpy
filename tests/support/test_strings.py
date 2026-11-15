from __future__ import annotations

import pytest

from swiftpy import helpers
from swiftpy.database.orm.model import Model
from swiftpy.support.strings import pluralize, snake_case


class TestSnakeCase:
    def test_pascal_case(self) -> None:
        assert snake_case("UserProfile") == "user_profile"
        assert snake_case("User") == "user"

    def test_camel_case(self) -> None:
        assert snake_case("userProfile") == "user_profile"
        assert snake_case("userId") == "user_id"

    def test_already_snake_case(self) -> None:
        assert snake_case("already_snake") == "already_snake"
        assert snake_case("user_profile") == "user_profile"
        assert snake_case("id") == "id"

    def test_acronym_runs(self) -> None:
        # Crucial bug fix from code review: APIKey must be api_key, NOT a_p_i_key
        assert snake_case("APIKey") == "api_key"
        assert snake_case("HTTPServer") == "http_server"
        assert snake_case("HTTPRequest") == "http_request"
        assert snake_case("XMLParser") == "xml_parser"
        assert snake_case("UserAPIKey") == "user_api_key"
        assert snake_case("OAuth2Client") == "o_auth2_client"
        assert snake_case("Oauth2Client") == "oauth2_client"

    def test_digits(self) -> None:
        assert snake_case("User2Profile") == "user2_profile"
        assert snake_case("v2Endpoint") == "v2_endpoint"
        assert snake_case("S3Bucket") == "s3_bucket"

    def test_empty_string(self) -> None:
        assert snake_case("") == ""

    def test_helper_reexport_parity(self) -> None:
        assert helpers.helper.snake_case("APIKey") == snake_case("APIKey")
        assert helpers.helper.snake_case("UserProfile") == snake_case("UserProfile")
        assert helpers.snake_case("APIKey") == snake_case("APIKey")
        assert helpers.pluralize("api_key") == pluralize("api_key")

    def test_orm_acronym_model_table_inference(self) -> None:
        class APIKey(Model):
            id: int
            key_prefix: str

        assert APIKey.__table__ == "api_keys"


class TestPluralize:
    @pytest.mark.parametrize(
        ("singular", "expected"),
        [
            ("user", "users"),
            ("company", "companies"),
            ("bus", "buses"),
            ("class", "classes"),
            ("box", "boxes"),
            ("analysis", "analyses"),
            ("axis", "axes"),
            ("database", "databases"),
            ("quiz", "quizzes"),
            ("mouse", "mice"),
            ("louse", "lice"),
            ("matrix", "matrices"),
            ("status", "statuses"),
            ("index", "indices"),
            ("sheep", "sheep"),
            ("aircraft", "aircraft"),
            ("equipment", "equipment"),
            ("information", "information"),
            ("person", "people"),
            ("child", "children"),
            ("ox", "oxen"),
        ],
    )
    def test_standard_and_irregular_pluralization(
        self, singular: str, expected: str
    ) -> None:
        assert pluralize(singular) == expected

    def test_capitalization_preservation(self) -> None:
        assert pluralize("Person") == "People"
        assert pluralize("Category") == "Categories"
        assert pluralize("USER") == "USERS"
        assert pluralize("PERSON") == "PEOPLE"

    def test_snake_cased_word(self) -> None:
        assert pluralize("user_profile") == "user_profiles"
        assert pluralize("api_key") == "api_keys"

    def test_empty_string(self) -> None:
        assert pluralize("") == ""

    def test_helper_reexport_parity(self) -> None:
        assert helpers.helper.pluralize("person") == pluralize("person")
        assert helpers.pluralize("person") == pluralize("person")
