from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import inspect
import itertools
import random
import sys
import uuid
from collections.abc import Callable, Mapping
from collections.abc import Sequence as TypingSequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import date as DateType
from pathlib import Path
from typing import (
    Any,
    ClassVar,
    Self,
    TypeGuard,
    cast,
    get_args,
    get_origin,
    overload,
)
from weakref import WeakKeyDictionary

from swiftpy.database.orm.model import Model, _set_factory_resolver
from swiftpy.database.orm.relationships import (
    BelongsTo,
    BelongsToMany,
    HasMany,
    HasOne,
    LoadedList,
    Relation,
)
from swiftpy.database.query_builder import Query
from swiftpy.database.transaction import db
from swiftpy.schemas.base import BaseSchema
from swiftpy.support.strings import snake_case

_MAX_FACTORY_DEPTH: int = 24
_FACTORY_DEPTH: ContextVar[int] = ContextVar("swiftpy_factory_depth", default=0)

type _StateCallback = (
    Callable[[dict[str, Any]], Mapping[str, Any]]
    | Callable[[dict[str, Any], Any], Mapping[str, Any]]
    | Callable[[], Mapping[str, Any]]
)
type _StateInput = Mapping[str, Any] | BaseSchema | Sequence | _StateCallback
type _RelatedFactoryTarget = (
    type[Factory[Any] | Model]
    | Factory[Any]
    | FactoryBatch[Any]
    | str
)
type _AttachTarget = (
    type[Factory[Any] | Model]
    | Factory[Any]
    | FactoryBatch[Any]
    | Model
    | TypingSequence[Model]
)
type _ParentTarget = type[Factory[Any] | Model] | Model | Factory[Any]

_FIRST_NAMES: tuple[str, ...] = (
    "Ada",
    "Alice",
    "Amara",
    "Bob",
    "Chidi",
    "Clara",
    "Daniel",
    "Elena",
    "Farah",
    "Gabriel",
    "Hannah",
    "Ibrahim",
    "Julia",
    "Kofi",
    "Liam",
    "Maya",
    "Noah",
    "Olivia",
    "Priya",
    "Rafael",
    "Sara",
    "Tariq",
    "Uma",
    "Victor",
    "Zara",
)

_LAST_NAMES: tuple[str, ...] = (
    "Adeyemi",
    "Alvarez",
    "Bauer",
    "Chen",
    "Cooper",
    "Davis",
    "Dubois",
    "Fischer",
    "Garcia",
    "Hassan",
    "Ito",
    "Kowalski",
    "Lovelace",
    "Mensah",
    "Nakamura",
    "Okafor",
    "Patel",
    "Rossi",
    "Salau",
    "Santos",
    "Silva",
    "Smith",
    "Tanaka",
    "Weber",
)

_WORDS: tuple[str, ...] = (
    "async",
    "atomic",
    "aurora",
    "beacon",
    "bridge",
    "cascade",
    "cipher",
    "cloud",
    "compact",
    "core",
    "crystal",
    "data",
    "delta",
    "durable",
    "echo",
    "engine",
    "event",
    "fabric",
    "fluent",
    "forge",
    "fusion",
    "gateway",
    "harbor",
    "horizon",
    "index",
    "kernel",
    "ledger",
    "matrix",
    "metric",
    "model",
    "nebula",
    "nexus",
    "orbit",
    "packet",
    "pilot",
    "pivot",
    "pulse",
    "query",
    "queue",
    "relay",
    "schema",
    "signal",
    "spark",
    "stream",
    "swift",
    "tensor",
    "token",
    "vector",
    "vertex",
    "worker",
)

_DOMAINS: tuple[str, ...] = (
    "example.com",
    "example.org",
    "example.net",
    "swiftpy.test",
)

_CITIES: tuple[str, ...] = (
    "Amsterdam",
    "Austin",
    "Berlin",
    "Chicago",
    "Lagos",
    "London",
    "Madrid",
    "Nairobi",
    "Osaka",
    "Ottawa",
    "Paris",
    "Seoul",
    "Singapore",
    "Toronto",
    "Zurich",
)

_COUNTRIES: tuple[str, ...] = (
    "Brazil",
    "Canada",
    "France",
    "Germany",
    "Ghana",
    "India",
    "Japan",
    "Kenya",
    "Netherlands",
    "Nigeria",
    "Singapore",
    "Spain",
    "Switzerland",
    "United Kingdom",
    "United States",
)

_COMPANY_SUFFIXES: tuple[str, ...] = (
    "Systems",
    "Labs",
    "Cloud",
    "Technologies",
    "Works",
    "Solutions",
    "Analytics",
    "Dynamics",
)


class Fake:
    """
    Zero-dependency, task-safe synthetic data generator for model factories.

    Security Note:
        `Fake` uses Python's deterministic `random.Random` for reproducible test and seed
        data. Never use `Fake` methods (such as `token_hex()`, `sha256()`, or `uuid4()`) to
        generate live authentication tokens, session secrets, API keys, or cryptographic salts.
    """

    def __init__(self, seed: int | None = None) -> None:
        self._rng = random.Random(seed)
        self._counter = itertools.count(1)

    def first_name(self) -> str:
        return self._rng.choice(_FIRST_NAMES)

    def last_name(self) -> str:
        return self._rng.choice(_LAST_NAMES)

    def name(self) -> str:
        return f"{self.first_name()} {self.last_name()}"

    def username(self) -> str:
        first = self.first_name().lower()
        last = self.last_name().lower()
        num = next(self._counter)
        return f"{first}.{last}{num}"

    def domain_name(self) -> str:
        return f"{self._rng.choice(_WORDS)}.{self._rng.choice(_DOMAINS)}"

    def email(self, domain: str | None = None) -> str:
        target_domain = domain or self._rng.choice(_DOMAINS)
        return f"{self.username()}@{target_domain}"

    def unique_email(self, domain: str = "example.com") -> str:
        seq = next(self._counter)
        suffix = self._rng.randint(1000, 9999)
        first = self.first_name().lower()
        return f"{first}.{seq}.{suffix}@{domain}"

    def safe_email(self) -> str:
        return self.unique_email(domain="example.org")

    def url(self, path: bool = True) -> str:
        base = f"https://{self.domain_name()}"
        if not path:
            return base
        return f"{base}/{self.slug(2)}"

    def word(self) -> str:
        return self._rng.choice(_WORDS)

    def words(self, nb: int = 3) -> list[str]:
        if isinstance(nb, bool) or not isinstance(nb, int) or nb < 1:
            raise ValueError("nb must be an integer >= 1.")
        return [self._rng.choice(_WORDS) for _ in range(nb)]

    def sentence(self, nb_words: int = 6, variable_nb_words: bool = True) -> str:
        if isinstance(nb_words, bool) or not isinstance(nb_words, int) or nb_words < 1:
            raise ValueError("nb_words must be an integer >= 1.")
        count = nb_words
        if variable_nb_words and nb_words > 2:
            low = max(2, nb_words - 2)
            high = nb_words + 2
            count = self._rng.randint(low, high)
        chosen = self.words(count)
        chosen[0] = chosen[0].capitalize()
        return " ".join(chosen) + "."

    def paragraph(
        self, nb_sentences: int = 3, variable_nb_sentences: bool = True
    ) -> str:
        if (
            isinstance(nb_sentences, bool)
            or not isinstance(nb_sentences, int)
            or nb_sentences < 1
        ):
            raise ValueError("nb_sentences must be an integer >= 1.")
        count = nb_sentences
        if variable_nb_sentences and nb_sentences > 1:
            count = self._rng.randint(max(1, nb_sentences - 1), nb_sentences + 2)
        return " ".join(self.sentence() for _ in range(count))

    def paragraphs(self, nb: int = 3) -> list[str]:
        if isinstance(nb, bool) or not isinstance(nb, int) or nb < 1:
            raise ValueError("nb must be an integer >= 1.")
        return [self.paragraph() for _ in range(nb)]

    def text(self, max_nb_chars: int = 200) -> str:
        if (
            isinstance(max_nb_chars, bool)
            or not isinstance(max_nb_chars, int)
            or max_nb_chars < 5
        ):
            raise ValueError("max_nb_chars must be an integer >= 5.")
        chunks: list[str] = []
        total = 0
        while total < max_nb_chars:
            sentence = self.sentence()
            added = len(sentence) + (1 if chunks else 0)
            if total + added > max_nb_chars:
                break
            chunks.append(sentence)
            total += added
        if not chunks:
            return self.sentence()[:max_nb_chars].rstrip()
        return " ".join(chunks)

    def slug(self, nb_words: int = 3) -> str:
        return "-".join(self.words(nb_words))

    def random_int(self, min_val: int = 0, max_val: int = 9999) -> int:
        return self._rng.randint(min_val, max_val)

    def random_float(
        self, min_val: float = 0.0, max_val: float = 1000.0, nb_decimals: int = 2
    ) -> float:
        return round(self._rng.uniform(min_val, max_val), nb_decimals)

    def boolean(self, chance_of_getting_true: int = 50) -> bool:
        return self._rng.randint(1, 100) <= chance_of_getting_true

    def random_element(self, elements: TypingSequence[Any]) -> Any:
        if not elements:
            raise ValueError("elements must not be empty.")
        return self._rng.choice(elements)

    def random_elements(
        self, elements: TypingSequence[Any], length: int = 2, unique: bool = False
    ) -> list[Any]:
        if isinstance(length, bool) or not isinstance(length, int) or length < 0:
            raise ValueError("length must be an integer >= 0.")
        if unique:
            if length > len(elements):
                raise ValueError(
                    "length cannot exceed number of available elements when unique=True."
                )
            return self._rng.sample(list(elements), length)
        if not elements and length > 0:
            raise ValueError("elements must not be empty.")
        return [self._rng.choice(elements) for _ in range(length)]

    def uuid_object(self) -> uuid.UUID:
        raw_bytes = bytes(self._rng.getrandbits(8) for _ in range(16))
        return uuid.UUID(bytes=raw_bytes, version=4)

    def uuid4(self) -> str:
        return str(self.uuid_object())

    def date_object(self, start_days_ago: int = 3650) -> DateType:
        days = self._rng.randint(0, max(0, start_days_ago))
        return (datetime.now(UTC) - timedelta(days=days)).date()

    def date(self, start_days_ago: int = 3650) -> str:
        return self.date_object(start_days_ago=start_days_ago).isoformat()

    def datetime_object(self, start_days_ago: int = 365) -> datetime:
        max_seconds = max(1, start_days_ago * 86400)
        offset = self._rng.randint(0, max_seconds)
        return datetime.now(UTC) - timedelta(seconds=offset)

    def iso8601(self, start_days_ago: int = 365) -> str:
        return self.datetime_object(start_days_ago=start_days_ago).isoformat()

    def phone_number(self) -> str:
        area = self._rng.randint(200, 999)
        mid = self._rng.randint(200, 999)
        end = self._rng.randint(1000, 9999)
        return f"+1-{area}-{mid}-{end}"

    def company(self) -> str:
        return f"{self.word().capitalize()} {self._rng.choice(_COMPANY_SUFFIXES)}"

    def city(self) -> str:
        return self._rng.choice(_CITIES)

    def country(self) -> str:
        return self._rng.choice(_COUNTRIES)

    def sha256(self) -> str:
        """Return a deterministic 64-character hex string for test/seed fixtures (not cryptographic)."""
        return self.token_hex(32)

    def token_hex(self, nbytes: int = 16) -> str:
        """Return a deterministic hex string for test/seed fixtures (do not use for live auth secrets)."""
        if isinstance(nbytes, bool) or not isinstance(nbytes, int) or nbytes < 1:
            raise ValueError("nbytes must be an integer >= 1.")
        return bytes(self._rng.getrandbits(8) for _ in range(nbytes)).hex()

    def binary(self, length: int = 64) -> bytes:
        if isinstance(length, bool) or not isinstance(length, int) or length < 0:
            raise ValueError("length must be an integer >= 0.")
        return bytes(self._rng.getrandbits(8) for _ in range(length))


class Sequence:
    """Cycles through fixed values/states or invokes a zero-based index callback."""

    @overload
    def __init__(self, first: Callable[[int], Any], /) -> None: ...

    @overload
    def __init__(self, first: object, *rest: object) -> None: ...

    def __init__(self, first: Any, *rest: Any) -> None:
        if not rest and callable(first):
            self._fn: Callable[[int], Any] | None = first
            self._items: tuple[Any, ...] = ()
        else:
            self._fn = None
            self._items = (first, *rest)

    def __call__(self, index: int) -> Any:
        if self._fn is not None:
            return self._fn(index)
        val = self._items[index % len(self._items)]
        if isinstance(val, (dict, list, set)):
            return copy.deepcopy(val)
        return val


@dataclass(frozen=True, slots=True)
class _HasRelationSpec:
    relation_name: str | None
    related: Any
    count: int | None
    state: Any
    kwargs: dict[str, Any]
    pivot_attributes: Any = None
    attach_only: bool = False


@dataclass(frozen=True, slots=True)
class _ForRelationSpec:
    relation_name: str | None
    parent: Any


_FACTORY_REGISTRY: WeakKeyDictionary[type[Any], type[Factory[Any]]] = (
    WeakKeyDictionary()
)


def register_factory(
    model_cls: type[Model],
    factory_cls: type[Factory[Any]],
    *,
    overwrite: bool = True,
) -> None:
    """Register a Factory subclass for a Model class."""
    if overwrite or model_cls not in _FACTORY_REGISTRY:
        _FACTORY_REGISTRY[model_cls] = factory_cls


def clear_factory_registry() -> None:
    """Clear registered model factories (intended for isolated test suites)."""
    _FACTORY_REGISTRY.clear()


def snapshot_factory_registry() -> dict[type[Any], type[Factory[Any]]]:
    """Return a shallow snapshot of registered model factories (intended for tests)."""
    return dict(_FACTORY_REGISTRY)


def restore_factory_registry(
    snapshot: Mapping[type[Any], type[Factory[Any]]],
) -> None:
    """Restore the model factory registry from a snapshot (intended for tests)."""
    _FACTORY_REGISTRY.clear()
    _FACTORY_REGISTRY.update(snapshot)


def _is_model_class(obj: Any) -> TypeGuard[type[Model]]:
    return (
        isinstance(obj, type)
        and hasattr(obj, "__table__")
        and hasattr(obj, "force_create")
        and hasattr(obj, "__relations_config__")
    )


def _is_model_instance(obj: Any) -> TypeGuard[Model]:
    return (
        not isinstance(obj, type)
        and hasattr(obj, "__table__")
        and hasattr(obj, "force_fill")
        and hasattr(obj, "get_primary_key")
    )


def _validate_positive_count(count: int) -> int:
    if isinstance(count, bool) or not isinstance(count, int):
        raise TypeError(f"Factory count must be an integer, got {type(count).__name__}.")
    if count < 1:
        raise ValueError(f"Factory count must be >= 1, got {count}.")
    return count


def _enter_factory_depth(model_name: str) -> Any:
    depth = _FACTORY_DEPTH.get()
    if depth >= _MAX_FACTORY_DEPTH:
        raise RecursionError(
            f"Maximum factory nesting depth ({_MAX_FACTORY_DEPTH}) exceeded while resolving "
            f"'{model_name}'; check for circular factory or relationship definitions."
        )
    return _FACTORY_DEPTH.set(depth + 1)


def _extract_mapping_payload(
    payload: Mapping[str, Any] | BaseSchema | None,
    kwargs: Mapping[str, Any],
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    if payload is not None:
        if isinstance(payload, BaseSchema):
            merged.update(payload.to_dict())
        elif isinstance(payload, Mapping):
            merged.update(payload)
        else:
            raise TypeError(
                f"Expected a mapping or BaseSchema payload, got {type(payload).__name__}."
            )
    merged.update(kwargs)
    return merged


def _callable_positional_arity(fn: Callable[..., Any]) -> int:
    """Return how many positional arguments (0, 1, or 2) a callable accepts."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return 0
    positional = 0
    has_varargs = False
    for param in sig.parameters.values():
        if param.kind == inspect.Parameter.VAR_POSITIONAL:
            has_varargs = True
        elif param.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            positional += 1
    if has_varargs and positional == 0:
        return 1
    return min(positional, 2)


def _invoke_with_context(
    fn: Callable[..., Any],
    attrs: dict[str, Any],
    index: int,
    parent: Any = None,
) -> Any:
    arity = _callable_positional_arity(fn)
    if arity == 0:
        return fn()
    if arity == 1:
        return fn(dict(attrs))
    if parent is not None:
        return fn(dict(attrs), parent)
    return fn(dict(attrs), index)


def _resolve_relation_on_model(model_cls: type[Any], name: str) -> Relation[Any]:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Relation name must be a non-empty string.")
    clean = name.strip()
    relations: Mapping[str, Relation[Any]] = getattr(
        model_cls, "__relations_config__", {}
    )
    if clean in relations:
        return relations[clean]
    clean_snake = snake_case(clean)
    if clean_snake in relations:
        return relations[clean_snake]
    for rel_name, rel in relations.items():
        if snake_case(rel_name) == clean_snake:
            return rel
    raise ValueError(
        f"Relation '{name}' is not defined on model '{model_cls.__name__}'."
    )


def _resolve_generic_model_arg(factory_cls: type[Any]) -> type[Any] | None:
    for base in getattr(factory_cls, "__orig_bases__", ()):
        origin = get_origin(base)
        if origin is None:
            continue
        if isinstance(origin, type) and issubclass(origin, Factory):
            args = get_args(base)
            if args and _is_model_class(args[0]):
                return cast(type[Any], args[0])
    return None


def _coerce_factory_instance(target: Any) -> Factory[Any]:
    if isinstance(target, FactoryBatch):
        return target._factory._clone()
    if isinstance(target, Factory):
        return target._clone()
    if isinstance(target, type) and issubclass(target, Factory):
        return target()
    if _is_model_class(target):
        return resolve_factory_for_model(target)
    raise TypeError(
        f"Expected a Factory, FactoryBatch, Factory class, or Model class; got {type(target).__name__}."
    )


def _load_factory_module_from_file(candidate_file: Path) -> Any:
    resolved_path = str(candidate_file.resolve())
    digest = hashlib.sha256(resolved_path.encode("utf-8")).hexdigest()[:12]
    synthetic_name = f"_swiftpy_factories.{candidate_file.stem}_{digest}"
    existing = sys.modules.get(synthetic_name)
    if existing is not None and getattr(existing, "__file__", None) == resolved_path:
        return existing
    spec = importlib.util.spec_from_file_location(synthetic_name, resolved_path)
    if not spec or not spec.loader:
        return None
    loaded_mod = importlib.util.module_from_spec(spec)
    sys.modules[synthetic_name] = loaded_mod
    try:
        spec.loader.exec_module(loaded_mod)
    except BaseException:
        sys.modules.pop(synthetic_name, None)
        raise
    return loaded_mod


def _discover_factory_class(model_cls: type[Model]) -> type[Factory[Any]] | None:
    """Attempt convention-based discovery of `<model>_factory.<Model>Factory`."""
    snake_model = snake_case(model_cls.__name__)
    expected_cls_name = f"{model_cls.__name__}Factory"

    candidates: list[tuple[str, Path]] = []
    mod_name = getattr(model_cls, "__module__", "") or ""
    if mod_name.startswith("app.models."):
        sub_parts = [p for p in mod_name.split(".")[2:-1] if p]
        if sub_parts:
            sub_dotted = ".".join(sub_parts)
            candidates.append(
                (
                    f"database.factories.{sub_dotted}.{snake_model}_factory",
                    Path.cwd()
                    / "database"
                    / "factories"
                    / Path(*sub_parts)
                    / f"{snake_model}_factory.py",
                )
            )
    elif "." in mod_name:
        mod_parts = [p for p in mod_name.split(".")[:-1] if p and p != "models"]
        if mod_parts:
            sub_dotted = ".".join(mod_parts)
            candidates.append(
                (
                    f"database.factories.{sub_dotted}.{snake_model}_factory",
                    Path.cwd()
                    / "database"
                    / "factories"
                    / Path(*mod_parts)
                    / f"{snake_model}_factory.py",
                )
            )

    candidates.append(
        (
            f"database.factories.{snake_model}_factory",
            Path.cwd() / "database" / "factories" / f"{snake_model}_factory.py",
        )
    )

    loaded_mod: Any = None
    loaded_mod_name = ""
    for candidate_mod_name, candidate_file in candidates:
        try:
            loaded_mod = importlib.import_module(candidate_mod_name)
            loaded_mod_name = candidate_mod_name
            break
        except ModuleNotFoundError as exc:
            missing = exc.name or ""
            is_missing_target = missing == candidate_mod_name or candidate_mod_name.startswith(
                f"{missing}."
            )
            if not is_missing_target:
                raise
            if candidate_file.is_file():
                loaded_mod = _load_factory_module_from_file(candidate_file)
                if loaded_mod is not None:
                    loaded_mod_name = str(candidate_file)
                    break

    if loaded_mod is None:
        return None

    candidate = getattr(loaded_mod, expected_cls_name, None)
    if candidate is not None:
        if (
            not isinstance(candidate, type)
            or not issubclass(candidate, Factory)
            or candidate is Factory
        ):
            raise TypeError(
                f"Factory '{expected_cls_name}' in '{loaded_mod_name}' must subclass Factory."
            )
        declared = getattr(candidate, "model", None)
        if declared is not model_cls:
            if declared is not None:
                raise ValueError(
                    f"Factory '{expected_cls_name}' in '{loaded_mod_name}' is bound to model "
                    f"'{getattr(declared, '__name__', declared)}', expected '{model_cls.__name__}'."
                )
            raise TypeError(
                f"Factory '{expected_cls_name}' in '{loaded_mod_name}' must declare 'model = {model_cls.__name__}'."
            )
        return candidate

    for attr in vars(loaded_mod).values():
        if (
            isinstance(attr, type)
            and issubclass(attr, Factory)
            and attr is not Factory
            and attr is not _DefaultModelFactory
            and getattr(attr, "model", None) is model_cls
        ):
            return attr
    return None


def resolve_factory_for_model[T: Model](model_cls: type[T]) -> Factory[T]:
    """Resolve the registered or convention-discovered Factory for `model_cls`."""
    explicit = model_cls.__dict__.get("__factory__")
    if explicit is not None:
        if (
            not isinstance(explicit, type)
            or not issubclass(explicit, Factory)
            or explicit is Factory
        ):
            raise TypeError(
                f"Invalid __factory__ on '{model_cls.__name__}': expected a Factory subclass, got {explicit!r}."
            )
        bound = getattr(explicit, "model", None)
        if (
            bound is not None
            and bound is not model_cls
            and not (isinstance(bound, type) and issubclass(model_cls, bound))
        ):
            raise ValueError(
                f"Factory '{explicit.__name__}' on '{model_cls.__name__}.__factory__' "
                f"targets '{getattr(bound, '__name__', bound)}' instead."
            )
        return cast(Factory[T], explicit())

    registered = _FACTORY_REGISTRY.get(model_cls)
    if registered is not None:
        return cast(Factory[T], registered())

    discovered = _discover_factory_class(model_cls)
    if discovered is not None:
        register_factory(model_cls, discovered, overwrite=False)
        return cast(Factory[T], discovered())

    return _DefaultModelFactory(model_cls)


def _make_has_relation_callable[F: Factory[Any]](
    factory: F, rel: Relation[Any]
) -> Callable[..., F]:
    resolved_name = rel.name

    def _has_relation(
        count: int = 1,
        state: _StateInput | None = None,
        **kwargs: Any,
    ) -> F:
        if not isinstance(rel, (HasMany, HasOne, BelongsToMany)):
            raise TypeError(
                f"Relationship '{resolved_name}' on '{factory._model_cls.__name__}' must be "
                f"HasMany, HasOne, or BelongsToMany for .has_{resolved_name}(), got {type(rel).__name__}."
            )
        target_model = rel.get_target_model()
        child_factory = resolve_factory_for_model(target_model)
        return factory.has(
            child_factory,
            count=count,
            state=state,
            relationship=resolved_name,
            **kwargs,
        )

    return _has_relation


def _make_for_relation_callable[F: Factory[Any]](
    factory: F, rel: BelongsTo[Any]
) -> Callable[[_ParentTarget], F]:
    resolved_name = rel.name

    def _for_relation(parent: _ParentTarget) -> F:
        return factory.for_(parent, relationship=resolved_name)

    return _for_relation


class Factory[T: Model]:
    """Declarative, immutable fluent factory for generating and seeding Active Record Models."""

    model: ClassVar[Any] = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.__name__ == "_DefaultModelFactory":
            return
        declared_model = cls.__dict__.get("model")
        if declared_model is None:
            declared_model = _resolve_generic_model_arg(cls)
            if declared_model is not None:
                cls.model = declared_model
        target_model = getattr(cls, "model", None)
        if _is_model_class(target_model):
            is_canonical_name = cls.__name__ == f"{target_model.__name__}Factory"
            register_factory(target_model, cls, overwrite=is_canonical_name)

    def __init__(
        self,
        *,
        model_cls: type[T] | None = None,
        fake: Fake | None = None,
    ) -> None:
        resolved = model_cls or getattr(self.__class__, "model", None)
        if not _is_model_class(resolved):
            raise TypeError(
                f"{type(self).__name__} must declare a 'model' Model subclass."
            )
        self._model_cls: type[T] = cast(type[T], resolved)
        self._states: list[Any] = []
        self._has_specs: list[_HasRelationSpec] = []
        self._for_specs: list[_ForRelationSpec] = []
        self._after_making: list[Callable[[T], Any]] = []
        self._after_creating: list[Callable[[T], Any]] = []
        self._fake: Fake = fake if fake is not None else Fake()

    @property
    def fake(self) -> Fake:
        """Return the isolated synthetic data generator for this factory chain."""
        return self._fake

    def _clone(self) -> Self:
        cloned = copy.copy(self)
        cloned._states = list(self._states)
        cloned._has_specs = list(self._has_specs)
        cloned._for_specs = list(self._for_specs)
        cloned._after_making = list(self._after_making)
        cloned._after_creating = list(self._after_creating)
        return cloned

    def seed(self, seed: int) -> Self:
        """Return a cloned factory configured with a deterministic PRNG seed."""
        cloned = self._clone()
        cloned._fake = Fake(seed=seed)
        return cloned

    def definition(self) -> dict[str, Any]:
        """Return default attribute definitions for generated model instances."""
        return {}

    def count(self, count: int) -> FactoryBatch[T]:
        """Return a batch factory configured to generate `count` model instances."""
        validated = _validate_positive_count(count)
        return FactoryBatch(self._clone(), validated)

    def state(
        self,
        state: _StateInput | None = None,
        /,
        **kwargs: Any,
    ) -> Self:
        """Apply state overrides (mapping, BaseSchema, Sequence, or callback) to generated models."""
        cloned = self._clone()
        if state is not None:
            if isinstance(state, BaseSchema):
                cloned._states.append(state.to_dict())
            elif isinstance(state, (Mapping, Sequence)) or callable(state):
                cloned._states.append(state)
            else:
                raise TypeError(
                    f"Factory state must be a mapping, BaseSchema, Sequence, or callable; got {type(state).__name__}."
                )
        if kwargs:
            cloned._states.append(dict(kwargs))
        return cloned

    def sequence(
        self,
        first: Mapping[str, Any] | Callable[[int], Mapping[str, Any]] | Sequence,
        *rest: Mapping[str, Any] | Callable[[int], Mapping[str, Any]],
    ) -> Self:
        """Cycle through attribute mappings or index callbacks across generated instances."""
        cloned = self._clone()
        if not rest and isinstance(first, Sequence):
            cloned._states.append(first)
        else:
            cloned._states.append(Sequence(first, *rest))
        return cloned

    def after_making(self, callback: Callable[[T], Any]) -> Self:
        """Register a callback executed after an instance is built in memory."""
        if not callable(callback):
            raise TypeError("after_making callback must be callable.")
        cloned = self._clone()
        cloned._after_making.append(callback)
        return cloned

    def after_creating(self, callback: Callable[[T], Any]) -> Self:
        """Register a sync or async callback executed after an instance is persisted."""
        if not callable(callback):
            raise TypeError("after_creating callback must be callable.")
        cloned = self._clone()
        cloned._after_creating.append(callback)
        return cloned

    def has(
        self,
        related: _RelatedFactoryTarget,
        count: int | None = None,
        state: _StateInput | None = None,
        *,
        relationship: str | None = None,
        **kwargs: Any,
    ) -> Self:
        """Attach related child/many-to-many models to generate alongside each parent."""
        if count is not None:
            _validate_positive_count(count)
        rel_name = relationship
        target_related: Any = related
        if isinstance(related, str):
            rel = _resolve_relation_on_model(self._model_cls, related)
            rel_name = rel.name
            target_related = resolve_factory_for_model(rel.get_target_model())

        if rel_name is not None:
            resolved_rel = _resolve_relation_on_model(self._model_cls, rel_name)
            if not isinstance(resolved_rel, (HasMany, HasOne, BelongsToMany)):
                raise TypeError(
                    f"Relationship '{resolved_rel.name}' on '{self._model_cls.__name__}' must be "
                    f"HasMany, HasOne, or BelongsToMany for .has(), got {type(resolved_rel).__name__}."
                )
            if isinstance(resolved_rel, HasOne) and count is not None and count != 1:
                raise ValueError(
                    f"HasOne relationship '{resolved_rel.name}' cannot create {count} records; expected 1."
                )

        cloned = self._clone()
        cloned._has_specs.append(
            _HasRelationSpec(
                relation_name=rel_name,
                related=target_related,
                count=count,
                state=state,
                kwargs=dict(kwargs),
                attach_only=False,
            )
        )
        return cloned

    def has_attached(
        self,
        related: _AttachTarget,
        pivot_attributes: Mapping[str, Any]
        | Callable[..., Mapping[str, Any]]
        | None = None,
        *,
        relationship: str | None = None,
    ) -> Self:
        """Attach related models via a BelongsToMany pivot table (creating them if a factory is given)."""
        cloned = self._clone()
        cloned._has_specs.append(
            _HasRelationSpec(
                relation_name=relationship,
                related=related,
                count=None,
                state=None,
                kwargs={},
                pivot_attributes=pivot_attributes,
                attach_only=True,
            )
        )
        return cloned

    def for_(
        self,
        parent: _ParentTarget,
        *,
        relationship: str | None = None,
    ) -> Self:
        """Associate each generated model with a parent BelongsTo model or factory."""
        if not (
            _is_model_instance(parent)
            or isinstance(parent, (Factory, FactoryBatch))
            or (isinstance(parent, type) and issubclass(parent, Factory))
            or _is_model_class(parent)
        ):
            raise TypeError(
                f"for_() expects a Model instance, Model class, Factory, or FactoryBatch; got {type(parent).__name__}."
            )
        if relationship is not None:
            parent_cls: type[Any] = (
                type(parent)
                if _is_model_instance(parent)
                else _coerce_factory_instance(parent)._model_cls
            )
            self._resolve_belongs_to(
                _ForRelationSpec(relation_name=relationship, parent=parent),
                parent_cls,
            )
        cloned = self._clone()
        cloned._for_specs.append(
            _ForRelationSpec(relation_name=relationship, parent=parent)
        )
        return cloned

    def __getattr__(self, name: str) -> Callable[..., Self]:
        if name.startswith("has_") and len(name) > 4:
            rel_candidate = name[4:]
            rel = self._try_resolve_relation(rel_candidate)
            if rel is not None:
                return _make_has_relation_callable(self, rel)

        if name.startswith("for_") and len(name) > 4:
            rel_candidate = name[4:]
            rel = self._try_resolve_relation(rel_candidate)
            if rel is not None and isinstance(rel, BelongsTo):
                return _make_for_relation_callable(self, rel)

        raise AttributeError(
            f"'{type(self).__name__}' object has no attribute '{name}'"
        )

    def _try_resolve_relation(self, name: str) -> Relation[Any] | None:
        try:
            return _resolve_relation_on_model(self._model_cls, name)
        except ValueError:
            return None

    def _resolve_has_relationship(
        self, spec: _HasRelationSpec
    ) -> tuple[Relation[Any], Factory[Any] | None, int, list[Any] | None]:
        """Resolve the target Relation, optional child Factory, count, and pre-built models."""
        if spec.attach_only:
            if _is_model_instance(spec.related):
                pre_models = [spec.related]
                target_cls = spec.related.__class__
                child_factory: Factory[Any] | None = None
                child_count = 1
            elif isinstance(spec.related, TypingSequence) and not isinstance(
                spec.related, (str, bytes, bytearray, memoryview)
            ):
                pre_models = list(spec.related)
                if not pre_models:
                    raise ValueError(
                        "has_attached() model sequence must not be empty unless relationship is empty."
                    )
                if not all(_is_model_instance(m) for m in pre_models):
                    raise TypeError(
                        "All elements in has_attached() sequence must be Model instances."
                    )
                target_cls = pre_models[0].__class__
                child_factory = None
                child_count = len(pre_models)
            else:
                pre_models = None
                child_factory = _coerce_factory_instance(spec.related)
                target_cls = child_factory._model_cls
                if isinstance(spec.related, FactoryBatch):
                    child_count = (
                        spec.related._count if spec.count is None else spec.count
                    )
                else:
                    child_count = 1 if spec.count is None else spec.count
        else:
            pre_models = None
            child_factory = _coerce_factory_instance(spec.related)
            target_cls = child_factory._model_cls
            if isinstance(spec.related, FactoryBatch):
                child_count = spec.related._count if spec.count is None else spec.count
            else:
                child_count = 1 if spec.count is None else spec.count
            if spec.state is not None or spec.kwargs:
                child_factory = child_factory.state(spec.state, **spec.kwargs)

        if spec.relation_name is not None:
            rel = _resolve_relation_on_model(self._model_cls, spec.relation_name)
        else:
            relations = getattr(self._model_cls, "__relations_config__", {})
            matches = [
                r
                for r in relations.values()
                if isinstance(r, (HasMany, HasOne, BelongsToMany))
                and (
                    r.get_target_model() is target_cls
                    or issubclass(target_cls, r.get_target_model())
                )
            ]
            if not matches:
                raise ValueError(
                    f"No HasOne, HasMany, or BelongsToMany relationship targeting "
                    f"'{target_cls.__name__}' found on '{self._model_cls.__name__}'."
                )
            if len(matches) > 1:
                raise ValueError(
                    f"Multiple relationships targeting '{target_cls.__name__}' found on "
                    f"'{self._model_cls.__name__}'; specify relationship=... or use .has_<relation>()."
                )
            rel = matches[0]

        if not isinstance(rel, (HasMany, HasOne, BelongsToMany)):
            raise TypeError(
                f"Relationship '{rel.name}' on '{self._model_cls.__name__}' must be "
                f"HasMany, HasOne, or BelongsToMany for .has(), got {type(rel).__name__}."
            )
        expected_target = rel.get_target_model()
        if not (
            target_cls is expected_target or issubclass(target_cls, expected_target)
        ):
            raise TypeError(
                f"Relationship '{rel.name}' on '{self._model_cls.__name__}' targets "
                f"'{expected_target.__name__}', got '{target_cls.__name__}'."
            )
        if isinstance(rel, HasOne) and child_count != 1:
            raise ValueError(
                f"HasOne relationship '{rel.name}' cannot create {child_count} records; expected 1."
            )
        return rel, child_factory, child_count, pre_models

    def _resolve_belongs_to(
        self, spec: _ForRelationSpec, parent_model_cls: type[Any]
    ) -> BelongsTo[Any]:
        if spec.relation_name is not None:
            rel = _resolve_relation_on_model(self._model_cls, spec.relation_name)
            if not isinstance(rel, BelongsTo):
                raise TypeError(
                    f"Relationship '{rel.name}' on '{self._model_cls.__name__}' must be "
                    f"BelongsTo for .for_(), got {type(rel).__name__}."
                )
            expected_parent = rel.get_target_model()
            if not (
                parent_model_cls is expected_parent
                or issubclass(parent_model_cls, expected_parent)
            ):
                raise TypeError(
                    f"Relationship '{rel.name}' on '{self._model_cls.__name__}' targets "
                    f"'{expected_parent.__name__}', got '{parent_model_cls.__name__}'."
                )
            return rel

        relations = getattr(self._model_cls, "__relations_config__", {})
        matches = [
            r
            for r in relations.values()
            if isinstance(r, BelongsTo)
            and (
                r.get_target_model() is parent_model_cls
                or issubclass(parent_model_cls, r.get_target_model())
            )
        ]
        if not matches:
            raise ValueError(
                f"No BelongsTo relationship targeting '{parent_model_cls.__name__}' "
                f"found on '{self._model_cls.__name__}'."
            )
        if len(matches) > 1:
            raise ValueError(
                f"Multiple BelongsTo relationships targeting '{parent_model_cls.__name__}' "
                f"found on '{self._model_cls.__name__}'; specify relationship=... or use .for_<relation>()."
            )
        return matches[0]

    def _prepare_parents_sync(self) -> tuple[dict[str, Any], dict[str, Any]]:
        fk_attrs: dict[str, Any] = {}
        loaded_parents: dict[str, Any] = {}
        for spec in self._for_specs:
            if _is_model_instance(spec.parent):
                parent_inst = spec.parent
            else:
                parent_factory = _coerce_factory_instance(spec.parent)
                parent_inst = parent_factory.make()
            rel = self._resolve_belongs_to(spec, parent_inst.__class__)
            target_cls = rel.get_target_model()
            fk = rel.foreign_key or f"{snake_case(rel.name)}_id"
            owner_key = rel._resolve_owner_key(target_cls)
            fk_val = getattr(parent_inst, owner_key, None)
            if fk_val is not None:
                fk_attrs[fk] = fk_val
            loaded_parents[rel.name] = parent_inst
        return fk_attrs, loaded_parents

    async def _prepare_parents_async(self) -> tuple[dict[str, Any], dict[str, Any]]:
        fk_attrs: dict[str, Any] = {}
        loaded_parents: dict[str, Any] = {}
        for spec in self._for_specs:
            if _is_model_instance(spec.parent):
                parent_inst = spec.parent
                rel = self._resolve_belongs_to(spec, parent_inst.__class__)
                target_cls = rel.get_target_model()
                owner_key = rel._resolve_owner_key(target_cls)
                if (
                    not getattr(parent_inst, "_exists", False)
                    and getattr(parent_inst, owner_key, None) is None
                ):
                    await parent_inst.save()
            else:
                parent_factory = _coerce_factory_instance(spec.parent)
                parent_inst = await parent_factory.create()
                rel = self._resolve_belongs_to(spec, parent_inst.__class__)
                target_cls = rel.get_target_model()
                owner_key = rel._resolve_owner_key(target_cls)
            fk = rel.foreign_key or f"{snake_case(rel.name)}_id"
            fk_attrs[fk] = getattr(parent_inst, owner_key, None)
            loaded_parents[rel.name] = parent_inst
        return fk_attrs, loaded_parents

    def _assemble_raw_attrs(
        self,
        index: int,
        parent_fk_attrs: Mapping[str, Any],
        overrides: Mapping[str, Any],
    ) -> dict[str, Any]:
        attrs = dict(self.definition())
        for state_entry in self._states:
            if isinstance(state_entry, Sequence):
                evaluated = state_entry(index)
                if not isinstance(evaluated, Mapping):
                    raise TypeError(
                        f"Sequence passed to Factory.state()/sequence() must yield a Mapping, got {type(evaluated).__name__}."
                    )
                attrs.update(evaluated)
            elif callable(state_entry):
                evaluated = _invoke_with_context(state_entry, attrs, index)
                if not isinstance(evaluated, Mapping):
                    raise TypeError(
                        f"State callable must return a Mapping, got {type(evaluated).__name__}."
                    )
                attrs.update(evaluated)
            elif isinstance(state_entry, Mapping):
                attrs.update(state_entry)
        attrs.update(parent_fk_attrs)
        attrs.update(overrides)
        return attrs

    def _resolve_attr_values_sync(
        self, attrs: dict[str, Any], index: int
    ) -> dict[str, Any]:
        resolved: dict[str, Any] = {}
        for key, val in attrs.items():
            if isinstance(val, Sequence):
                resolved[key] = val(index)
            elif isinstance(val, FactoryBatch):
                raise TypeError(
                    f"Attribute '{key}' cannot hold a FactoryBatch; use a single Factory for foreign-key columns."
                )
            elif isinstance(val, Factory) or (
                isinstance(val, type) and issubclass(val, Factory)
            ) or _is_model_class(val):
                nested_factory = _coerce_factory_instance(val)
                nested_inst = nested_factory.make()
                resolved[key] = nested_inst.get_primary_key()
            elif callable(val) and not isinstance(val, type):
                res = _invoke_with_context(val, resolved, index)
                if inspect.isawaitable(res):
                    if inspect.iscoroutine(res):
                        res.close()
                    raise TypeError(
                        f"Attribute callable for '{key}' returned an awaitable during synchronous make()/raw()."
                    )
                resolved[key] = res
            else:
                resolved[key] = val
        return resolved

    async def _resolve_attr_values_async(
        self, attrs: dict[str, Any], index: int
    ) -> dict[str, Any]:
        resolved: dict[str, Any] = {}
        for key, val in attrs.items():
            if isinstance(val, Sequence):
                resolved[key] = val(index)
            elif isinstance(val, FactoryBatch):
                raise TypeError(
                    f"Attribute '{key}' cannot hold a FactoryBatch; use a single Factory for foreign-key columns."
                )
            elif isinstance(val, Factory) or (
                isinstance(val, type) and issubclass(val, Factory)
            ) or _is_model_class(val):
                nested_factory = _coerce_factory_instance(val)
                nested_inst = await nested_factory.create()
                resolved[key] = nested_inst.get_primary_key()
            elif callable(val) and not isinstance(val, type):
                res = _invoke_with_context(val, resolved, index)
                if inspect.isawaitable(res):
                    res = await res
                resolved[key] = res
            else:
                resolved[key] = val
        return resolved

    def _raw_many(
        self,
        count: int,
        attributes: Mapping[str, Any] | BaseSchema | None = None,
        /,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        depth_token = _enter_factory_depth(self._model_cls.__name__)
        try:
            overrides = _extract_mapping_payload(attributes, kwargs)
            parent_fk_attrs, _ = self._prepare_parents_sync()
            results: list[dict[str, Any]] = []
            for idx in range(count):
                raw_attrs = self._assemble_raw_attrs(idx, parent_fk_attrs, overrides)
                results.append(self._resolve_attr_values_sync(raw_attrs, idx))
            return results
        finally:
            _FACTORY_DEPTH.reset(depth_token)

    def _make_many(
        self,
        count: int,
        attributes: Mapping[str, Any] | BaseSchema | None = None,
        /,
        **kwargs: Any,
    ) -> list[T]:
        depth_token = _enter_factory_depth(self._model_cls.__name__)
        try:
            overrides = _extract_mapping_payload(attributes, kwargs)
            parent_fk_attrs, loaded_parents = self._prepare_parents_sync()
            instances: list[T] = []
            for idx in range(count):
                raw_attrs = self._assemble_raw_attrs(idx, parent_fk_attrs, overrides)
                resolved_attrs = self._resolve_attr_values_sync(raw_attrs, idx)
                instance = self._model_cls()
                instance.force_fill(**resolved_attrs)
                for rel_name, parent_obj in loaded_parents.items():
                    instance._loaded_relations[rel_name] = parent_obj

                for spec in self._has_specs:
                    rel, child_factory, child_count, pre_models = (
                        self._resolve_has_relationship(spec)
                    )
                    if isinstance(rel, (HasMany, HasOne)):
                        assert child_factory is not None
                        owner_name = (
                            rel.owner_cls.__name__
                            if rel.owner_cls is not None
                            else instance.__class__.__name__
                        )
                        fk = rel.foreign_key or f"{snake_case(owner_name)}_id"
                        local_key = rel._resolve_local_key(instance)
                        parent_pk = getattr(instance, local_key, None)
                        fk_override = {fk: parent_pk} if parent_pk is not None else {}
                        scoped_child_factory = self._bind_parent_context_state(
                            child_factory, fk_override, instance
                        )
                        children = scoped_child_factory._make_many(child_count)
                        self._attach_loaded_children(instance, rel, children)
                    elif isinstance(rel, BelongsToMany):
                        children = (
                            list(pre_models)
                            if pre_models is not None
                            else cast(Factory[Any], child_factory)._make_many(child_count)
                        )
                        self._attach_loaded_children(instance, rel, children)

                for hook in self._after_making:
                    hook_res = hook(instance)
                    if inspect.isawaitable(hook_res):
                        if inspect.iscoroutine(hook_res):
                            hook_res.close()
                        raise TypeError(
                            "after_making callback cannot be async when calling synchronous make()."
                        )
                instances.append(instance)
            return instances
        finally:
            _FACTORY_DEPTH.reset(depth_token)

    async def _create_many(
        self,
        count: int,
        attributes: Mapping[str, Any] | BaseSchema | None = None,
        /,
        **kwargs: Any,
    ) -> list[T]:
        depth_token = _enter_factory_depth(self._model_cls.__name__)
        try:
            async with db.transaction():
                return await self._create_many_in_tx(count, attributes, **kwargs)
        finally:
            _FACTORY_DEPTH.reset(depth_token)

    async def _create_many_in_tx(
        self,
        count: int,
        attributes: Mapping[str, Any] | BaseSchema | None = None,
        /,
        **kwargs: Any,
    ) -> list[T]:
        overrides = _extract_mapping_payload(attributes, kwargs)
        parent_fk_attrs, loaded_parents = await self._prepare_parents_async()
        instances: list[T] = []

        for idx in range(count):
            raw_attrs = self._assemble_raw_attrs(idx, parent_fk_attrs, overrides)
            resolved_attrs = await self._resolve_attr_values_async(raw_attrs, idx)
            instance = self._model_cls()
            instance.force_fill(**resolved_attrs)
            for rel_name, parent_obj in loaded_parents.items():
                instance._loaded_relations[rel_name] = parent_obj

            for hook in self._after_making:
                hook_res = hook(instance)
                if inspect.isawaitable(hook_res):
                    await hook_res

            await instance.save()

            for spec in self._has_specs:
                rel, child_factory, child_count, pre_models = (
                    self._resolve_has_relationship(spec)
                )
                if isinstance(rel, (HasMany, HasOne)):
                    assert child_factory is not None
                    owner_name = (
                        rel.owner_cls.__name__
                        if rel.owner_cls is not None
                        else instance.__class__.__name__
                    )
                    fk = rel.foreign_key or f"{snake_case(owner_name)}_id"
                    local_key = rel._resolve_local_key(instance)
                    parent_pk = getattr(instance, local_key, None)
                    scoped_child_factory = self._bind_parent_context_state(
                        child_factory, {fk: parent_pk}, instance
                    )
                    children = await scoped_child_factory._create_many(child_count)
                    self._attach_loaded_children(instance, rel, children)
                elif isinstance(rel, BelongsToMany):
                    if pre_models is not None:
                        children = list(pre_models)
                        for child in children:
                            if not getattr(child, "_exists", False):
                                await child.save()
                    else:
                        assert child_factory is not None
                        scoped_child_factory = self._bind_parent_context_state(
                            child_factory, {}, instance
                        )
                        children = await scoped_child_factory._create_many(child_count)

                    await self._attach_pivot_records(
                        instance, rel, children, spec.pivot_attributes
                    )
                    self._attach_loaded_children(instance, rel, children)

            for hook in self._after_creating:
                hook_res = hook(instance)
                if inspect.isawaitable(hook_res):
                    await hook_res

            instances.append(instance)

        return instances

    @staticmethod
    def _bind_parent_context_state(
        child_factory: Factory[Any],
        fk_override: dict[str, Any],
        parent_instance: Model,
    ) -> Factory[Any]:
        """Inject parent foreign key and bind 2-arg state callables to `(attrs, parent)`."""
        adapted_states: list[Any] = []
        for entry in child_factory._states:
            if (
                callable(entry)
                and not isinstance(entry, Sequence)
                and _callable_positional_arity(entry) >= 2
            ):
                fn = entry

                def _bound_parent_state(
                    attrs: dict[str, Any],
                    _idx: int,
                    _fn: Callable[..., Any] = fn,
                    _parent: Model = parent_instance,
                ) -> Any:
                    return _fn(attrs, _parent)

                adapted_states.append(_bound_parent_state)
                continue
            adapted_states.append(entry)

        cloned = child_factory._clone()
        cloned._states = adapted_states
        if fk_override:
            cloned._states.append(fk_override)
        return cloned

    @staticmethod
    def _attach_loaded_children(
        parent: Model, rel: Relation[Any], children: list[Any]
    ) -> None:
        if isinstance(rel, HasOne):
            parent._loaded_relations[rel.name] = children[0] if children else None
            return
        existing = parent._loaded_relations.get(rel.name)
        if isinstance(existing, LoadedList):
            existing.extend(children)
        else:
            parent._loaded_relations[rel.name] = LoadedList(children)

    @staticmethod
    async def _attach_pivot_records(
        parent: Model,
        rel: BelongsToMany[Any],
        children: list[Any],
        pivot_attributes: Any,
    ) -> None:
        target_model = rel.get_target_model()
        parent_cls = rel.owner_cls or parent.__class__
        parent_key = rel._resolve_parent_key(parent_cls)
        related_key = rel._resolve_related_key(target_model)
        pivot_table = rel._resolve_pivot_table(parent_cls)
        fpk = rel.foreign_pivot_key or f"{snake_case(parent_cls.__name__)}_id"
        rpk = rel.related_pivot_key or f"{snake_case(target_model.__name__)}_id"
        parent_id = getattr(parent, parent_key, None)

        pivot_rows: list[dict[str, Any]] = []
        for idx, child in enumerate(children):
            child_id = getattr(child, related_key, None)
            extra: dict[str, Any] = {}
            if pivot_attributes is not None:
                if callable(pivot_attributes):
                    arity = _callable_positional_arity(pivot_attributes)
                    if arity == 0:
                        evaluated = pivot_attributes()
                    elif arity == 1:
                        evaluated = pivot_attributes(child)
                    else:
                        evaluated = pivot_attributes(parent, child)
                    if not isinstance(evaluated, Mapping):
                        raise TypeError(
                            f"pivot_attributes callable must return a Mapping, got {type(evaluated).__name__}."
                        )
                    extra.update(evaluated)
                elif isinstance(pivot_attributes, Mapping):
                    extra.update(pivot_attributes)
                else:
                    raise TypeError(
                        f"pivot_attributes must be a Mapping or callable, got {type(pivot_attributes).__name__}."
                    )
            _ = idx
            pivot_rows.append({fpk: parent_id, rpk: child_id, **extra})

        for row_data in pivot_rows:
            await Query.table(pivot_table).insert(row_data)

    def raw(
        self,
        attributes: Mapping[str, Any] | BaseSchema | None = None,
        /,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Return a single resolved attribute dictionary without instantiating a model."""
        return self._raw_many(1, attributes, **kwargs)[0]

    def raw_many(
        self,
        count: int,
        attributes: Mapping[str, Any] | BaseSchema | None = None,
        /,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Return `count` resolved attribute dictionaries without instantiating models."""
        validated = _validate_positive_count(count)
        return self._raw_many(validated, attributes, **kwargs)

    def make(
        self,
        attributes: Mapping[str, Any] | BaseSchema | None = None,
        /,
        **kwargs: Any,
    ) -> T:
        """Build a single unsaved Model instance in memory without writing to the database."""
        return self._make_many(1, attributes, **kwargs)[0]

    def make_one(
        self,
        attributes: Mapping[str, Any] | BaseSchema | None = None,
        /,
        **kwargs: Any,
    ) -> T:
        """Alias for `make()` returning a single unsaved Model instance."""
        return self.make(attributes, **kwargs)

    def make_many(
        self,
        count: int,
        attributes: Mapping[str, Any] | BaseSchema | None = None,
        /,
        **kwargs: Any,
    ) -> list[T]:
        """Build `count` unsaved Model instances in memory without writing to the database."""
        validated = _validate_positive_count(count)
        return self._make_many(validated, attributes, **kwargs)

    async def create(
        self,
        attributes: Mapping[str, Any] | BaseSchema | None = None,
        /,
        **kwargs: Any,
    ) -> T:
        """Create and persist a single Model instance (and any `.has()` relations) in the database."""
        created = await self._create_many(1, attributes, **kwargs)
        return created[0]

    async def create_one(
        self,
        attributes: Mapping[str, Any] | BaseSchema | None = None,
        /,
        **kwargs: Any,
    ) -> T:
        """Alias for `create()` returning a single persisted Model instance."""
        return await self.create(attributes, **kwargs)

    async def create_many(
        self,
        count: int,
        attributes: Mapping[str, Any] | BaseSchema | None = None,
        /,
        **kwargs: Any,
    ) -> list[T]:
        """Create and persist `count` Model instances in the database."""
        validated = _validate_positive_count(count)
        return await self._create_many(validated, attributes, **kwargs)


class FactoryBatch[T: Model]:
    """Count-configured fluent factory builder whose terminal methods return `list[T]`."""

    def __init__(self, factory: Factory[T], count: int) -> None:
        self._factory: Factory[T] = factory
        self._count: int = _validate_positive_count(count)

    @property
    def fake(self) -> Fake:
        return self._factory.fake

    def seed(self, seed: int) -> FactoryBatch[T]:
        return FactoryBatch(self._factory.seed(seed), self._count)

    def count(self, count: int) -> FactoryBatch[T]:
        return FactoryBatch(self._factory._clone(), _validate_positive_count(count))

    def state(
        self,
        state: _StateInput | None = None,
        /,
        **kwargs: Any,
    ) -> FactoryBatch[T]:
        return FactoryBatch(self._factory.state(state, **kwargs), self._count)

    def sequence(
        self,
        first: Mapping[str, Any] | Callable[[int], Mapping[str, Any]] | Sequence,
        *rest: Mapping[str, Any] | Callable[[int], Mapping[str, Any]],
    ) -> FactoryBatch[T]:
        return FactoryBatch(self._factory.sequence(first, *rest), self._count)

    def after_making(self, callback: Callable[[T], Any]) -> FactoryBatch[T]:
        return FactoryBatch(self._factory.after_making(callback), self._count)

    def after_creating(self, callback: Callable[[T], Any]) -> FactoryBatch[T]:
        return FactoryBatch(self._factory.after_creating(callback), self._count)

    def has(
        self,
        related: _RelatedFactoryTarget,
        count: int | None = None,
        state: _StateInput | None = None,
        *,
        relationship: str | None = None,
        **kwargs: Any,
    ) -> FactoryBatch[T]:
        return FactoryBatch(
            self._factory.has(
                related,
                count=count,
                state=state,
                relationship=relationship,
                **kwargs,
            ),
            self._count,
        )

    def has_attached(
        self,
        related: _AttachTarget,
        pivot_attributes: Mapping[str, Any]
        | Callable[..., Mapping[str, Any]]
        | None = None,
        *,
        relationship: str | None = None,
    ) -> FactoryBatch[T]:
        return FactoryBatch(
            self._factory.has_attached(
                related,
                pivot_attributes=pivot_attributes,
                relationship=relationship,
            ),
            self._count,
        )

    def for_(
        self,
        parent: _ParentTarget,
        *,
        relationship: str | None = None,
    ) -> FactoryBatch[T]:
        return FactoryBatch(
            self._factory.for_(parent, relationship=relationship), self._count
        )

    def __getattr__(self, name: str) -> Callable[..., FactoryBatch[T]]:
        attr = getattr(self._factory, name)
        if callable(attr):

            def _delegated(*args: Any, **kwargs: Any) -> FactoryBatch[T]:
                result = attr(*args, **kwargs)
                if isinstance(result, Factory):
                    return FactoryBatch(result, self._count)
                raise TypeError(
                    f"Factory method '{name}' returned {type(result).__name__} instead of a Factory."
                )

            return _delegated
        raise AttributeError(
            f"'{type(self).__name__}' object has no attribute '{name}'"
        )

    def raw(
        self,
        attributes: Mapping[str, Any] | BaseSchema | None = None,
        /,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Return `count` resolved attribute dictionaries without instantiating models."""
        return self._factory._raw_many(self._count, attributes, **kwargs)

    def make(
        self,
        attributes: Mapping[str, Any] | BaseSchema | None = None,
        /,
        **kwargs: Any,
    ) -> list[T]:
        """Build `count` unsaved Model instances in memory."""
        return self._factory._make_many(self._count, attributes, **kwargs)

    async def create(
        self,
        attributes: Mapping[str, Any] | BaseSchema | None = None,
        /,
        **kwargs: Any,
    ) -> list[T]:
        """Create and persist `count` Model instances (and any `.has()` relations) in the database."""
        return await self._factory._create_many(self._count, attributes, **kwargs)


class _DefaultModelFactory[T: Model](Factory[T]):
    """Fallback factory used when a Model has no dedicated Factory subclass."""

    def __init__(self, model_cls: type[T], *, fake: Fake | None = None) -> None:
        super().__init__(model_cls=model_cls, fake=fake)


_set_factory_resolver(resolve_factory_for_model)

__all__ = [
    "Factory",
    "FactoryBatch",
    "Fake",
    "Sequence",
    "clear_factory_registry",
    "register_factory",
    "resolve_factory_for_model",
    "restore_factory_registry",
    "snapshot_factory_registry",
]
