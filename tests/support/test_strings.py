from __future__ import annotations

import pytest

from swiftpy.helpers.helper import pluralize as helper_pluralize
from swiftpy.helpers.helper import snake_case as helper_snake_case
from swiftpy.support.strings import pluralize, snake_case


class TestSnakeCase:
    def test_pascal_case(self) -> None:
        assert snake_case("UserProfile") == "user_profile"
        assert snake_case("User") == "user"

    def test_camel_case(self) -> None:
        assert snake_case("userProfile") == "user_profile"
        assert snake_case("userId") == "user_id"

    def test_already_snake_case(self) -> None:
        assert snake_case("user_profile") == "user_profile"
        assert snake_case("id") == "id"

    def test_acronym_runs(self) -> None:
        # Crucial bug fix from code review: APIKey must be api_key, NOT a_p_i_key
        assert snake_case("APIKey") == "api_key"
        assert snake_case("HTTPRequest") == "http_request"
        assert snake_case("XMLParser") == "xml_parser"
        assert snake_case("UserAPIKey") == "user_api_key"
        assert snake_case("OAuth2Client") == "o_auth2_client"
        assert snake_case("Oauth2Client") == "oauth2_client"

    def test_empty_string(self) -> None:
        assert snake_case("") == ""

    def test_helper_reexport_parity(self) -> None:
        assert helper_snake_case("APIKey") == snake_case("APIKey")
        assert helper_snake_case("UserProfile") == snake_case("UserProfile")


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
        assert helper_pluralize("person") == pluralize("person")
