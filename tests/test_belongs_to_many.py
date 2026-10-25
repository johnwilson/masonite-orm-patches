import pytest
from masoniteorm.collection import Collection
from masoniteorm.connections.SQLiteConnection import SQLiteConnection
from masoniteorm.models import Model
from masoniteorm.relationships import belongs_to_many


class User(Model):
    __table__ = "users"
    __fillable__ = ["name"]
    __timestamps__ = False

    @belongs_to_many("user_id", "role_id", "id", "id", table="role_user")
    def roles(self):
        return Role


class Role(Model):
    __table__ = "roles"
    __fillable__ = ["name"]
    __timestamps__ = False


class RoleUser(Model):
    __table__ = "role_user"
    __fillable__ = ["user_id", "role_id"]
    __timestamps__ = False


@pytest.fixture
def seeded(database):
    with database.create("users") as table:
        table.increments("id").primary()
        table.string("name")
    with database.create("roles") as table:
        table.increments("id").primary()
        table.string("name")
    with database.create("role_user") as table:
        table.increments("id").primary()
        table.integer("user_id")
        table.integer("role_id")

    ada, grace, linus = (User.create({"name": n}) for n in ("Ada", "Grace", "Linus"))
    admin, editor = (Role.create({"name": n}) for n in ("admin", "editor"))
    for user, role in [(ada, admin), (ada, editor), (grace, editor)]:
        RoleUser.create({"user_id": user.id, "role_id": role.id})


@pytest.fixture
def selects(monkeypatch):
    """Record every SELECT statement sent to SQLite."""
    statements = []
    original = SQLiteConnection.query

    def query(self, query, bindings=(), results="*"):
        if query.lstrip().upper().startswith("SELECT"):
            statements.append(query)
        return original(self, query, bindings, results)

    monkeypatch.setattr(SQLiteConnection, "query", query)
    return statements


def role_names(user):
    return sorted(role.name for role in user.roles)


def test_upstream_raises_without_patch(seeded):
    with pytest.raises(NotImplementedError):
        User.with_("roles").get()


@pytest.mark.usefixtures("patched")
def test_eager_loads_roles_for_every_user_in_one_query(seeded, selects):
    users = User.with_("roles").order_by("id").get()

    assert [u.name for u in users] == ["Ada", "Grace", "Linus"]
    pivot_queries = [q for q in selects if "role_user" in q]
    assert len(pivot_queries) == 1

    by_name = {u.name: u for u in users}
    assert role_names(by_name["Ada"]) == ["admin", "editor"]
    assert role_names(by_name["Grace"]) == ["editor"]
    # Accessing the relation must not trigger a lazy query.
    assert len([q for q in selects if "role_user" in q]) == 1


@pytest.mark.usefixtures("patched")
def test_user_without_roles_gets_empty_collection(seeded):
    users = User.with_("roles").get()
    linus = next(u for u in users if u.name == "Linus")

    assert isinstance(linus.roles, Collection)
    assert linus.roles.count() == 0


@pytest.mark.usefixtures("patched")
def test_eager_loaded_roles_carry_pivot(seeded):
    ada = next(u for u in User.with_("roles").get() if u.name == "Ada")

    for role in ada.roles:
        # BelongsToMany stores the pivot on the original attributes only.
        pivot = role.get_original("pivot")
        assert pivot.user_id == ada.id
        assert pivot.role_id == role.id


@pytest.mark.usefixtures("patched")
def test_constrained_eager_load(seeded):
    users = User.with_({"roles": lambda q: q.where("roles.name", "editor")}).get()

    assert {u.name: role_names(u) for u in users} == {
        "Ada": ["editor"],
        "Grace": ["editor"],
        "Linus": [],
    }


@pytest.mark.usefixtures("patched")
def test_matches_lazy_loading(seeded):
    eager = {u.name: role_names(u) for u in User.with_("roles").get()}
    lazy = {u.name: role_names(u) for u in User.all()}
    assert eager == lazy
