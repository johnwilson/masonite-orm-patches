import pytest
from masoniteorm.collection import Collection
from masoniteorm.models import Model
from masoniteorm.relationships import belongs_to, belongs_to_many, has_many, has_one


class User(Model):
    __table__ = "users"
    __fillable__ = ["name"]
    __timestamps__ = False

    @belongs_to_many("user_id", "role_id", "id", "id", table="role_user")
    def roles(self):
        return Role

    @has_many("id", "user_id")
    def posts(self):
        return Post

    @has_one("user_id", "id")
    def profile(self):
        return Profile


class Role(Model):
    __table__ = "roles"
    __fillable__ = ["name"]
    __timestamps__ = False


class RoleUser(Model):
    __table__ = "role_user"
    __fillable__ = ["user_id", "role_id"]
    __timestamps__ = False


class Post(Model):
    __table__ = "posts"
    __fillable__ = ["user_id", "title"]
    __timestamps__ = False

    @belongs_to("user_id", "id")
    def author(self):
        return User


class Profile(Model):
    __table__ = "profiles"
    __fillable__ = ["user_id", "bio"]
    __timestamps__ = False


@pytest.fixture
def seeded(database):
    tables = {
        "users": ["name"],
        "roles": ["name"],
        "role_user": ["user_id:int", "role_id:int"],
        "posts": ["user_id:int:nullable", "title"],
        "profiles": ["user_id:int", "bio"],
    }
    for name, columns in tables.items():
        with database.create(name) as table:
            table.increments("id").primary()
            for column in columns:
                column_name, *kind = column.split(":")
                if "int" in kind:
                    field = table.integer(column_name)
                else:
                    field = table.string(column_name)
                if "nullable" in kind:
                    field.nullable()

    ada = User.create({"name": "Ada"})
    User.create({"name": "Linus"})
    admin = Role.create({"name": "admin"})
    RoleUser.create({"user_id": ada.id, "role_id": admin.id})
    Post.create({"user_id": ada.id, "title": "Notes"})
    Post.create({"user_id": None, "title": "Orphan"})
    Profile.create({"user_id": ada.id, "bio": "Mathematician"})


def test_upstream_returns_none_for_empty_to_many(seeded):
    linus = User.with_("roles").where("name", "Linus").first()
    assert linus.roles is None


@pytest.mark.usefixtures("patched")
@pytest.mark.parametrize("relation", ["roles", "posts"])
def test_first_without_rows_gets_empty_collection(seeded, relation):
    linus = User.with_(relation).where("name", "Linus").first()

    related = getattr(linus, relation)
    assert isinstance(related, Collection)
    assert related.count() == 0


@pytest.mark.usefixtures("patched")
@pytest.mark.parametrize("relation", ["roles", "posts"])
def test_first_with_rows_is_unchanged(seeded, relation):
    ada = User.with_(relation).where("name", "Ada").first()
    assert getattr(ada, relation).count() == 1


@pytest.mark.usefixtures("patched")
def test_get_where_no_parent_has_rows_gives_each_its_own_collection(seeded):
    RoleUser.where("id", ">", 0).delete()
    users = User.with_("roles").get()

    roles = [user.roles for user in users]
    assert all(isinstance(r, Collection) and r.count() == 0 for r in roles)
    assert roles[0] is not roles[1]


@pytest.mark.usefixtures("patched")
def test_to_one_relations_still_resolve_to_none(seeded):
    linus = User.with_("profile").where("name", "Linus").first()
    orphan = Post.with_("author").where("title", "Orphan").first()

    assert linus.profile is None
    assert orphan.author is None
