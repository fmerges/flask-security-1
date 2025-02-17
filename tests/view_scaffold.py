# :copyright: (c) 2019-2021 by J. Christopher Wagner (jwag).
# :license: MIT, see LICENSE for more details.

"""
This is a simple scaffold that can be run as an app and manually test
various views using a browser.
It can be used to test translations by adding ?lang=xx. You might need to
delete the session cookie if you need to switch between languages (it is easy to
do this with your browser development tools).

Configurations can be set via environment variables.

Runs on port 5001

An initial user: test@test.com/password is created.
If you want to register a new user - you will receive a 'flash' that has the
confirm URL (with token) you need to enter into your browser address bar.

Since we don't actually send email - we have signal handlers flash the required
data and a mail sender that flashes what mail would be sent!

"""

import datetime
import os
import typing as t

from flask import Flask, flash, render_template_string, request, session
from flask_sqlalchemy import SQLAlchemy

from flask.json import JSONEncoder
from flask_security import (
    Security,
    WebauthnUtil,
    auth_required,
    current_user,
    login_required,
    SQLAlchemyUserDatastore,
)
from flask_security.models import fsqla_v3 as fsqla
from flask_security.signals import (
    us_security_token_sent,
    tf_security_token_sent,
    reset_password_instructions_sent,
    user_registered,
)
from flask_security.utils import hash_password, uia_email_mapper, uia_phone_mapper


def _find_bool(v):
    if str(v).lower() in ["true"]:
        return True
    elif str(v).lower() in ["false"]:
        return False
    return v


class FlashMail:
    def __init__(self, app):
        app.extensions["mail"] = self

    def send(self, msg):
        flash(msg.body)


def create_app():
    # Use real templates - not test templates...
    app = Flask(
        "view_scaffold", template_folder="../", static_folder="../flask_security/static"
    )
    app.config["DEBUG"] = True
    # SECRET_KEY generated using: secrets.token_urlsafe()
    app.config["SECRET_KEY"] = "pf9Wkove4IKEAXvy-cQkeDPhv9Cb3Ag-wyJILbq_dFw"
    # PASSWORD_SALT secrets.SystemRandom().getrandbits(128)
    app.config["SECURITY_PASSWORD_SALT"] = "156043940537155509276282232127182067465"

    app.config["LOGIN_DISABLED"] = False
    app.config["WTF_CSRF_ENABLED"] = True
    app.config["SECURITY_USER_IDENTITY_ATTRIBUTES"] = [
        {"email": {"mapper": uia_email_mapper, "case_insensitive": True}},
        {"us_phone_number": {"mapper": uia_phone_mapper}},
    ]
    # app.config["SECURITY_US_ENABLED_METHODS"] = ["password"]
    # app.config["SECURITY_US_ENABLED_METHODS"] = ["authenticator", "password"]

    # app.config["SECURITY_US_SIGNIN_REPLACES_LOGIN"] = True

    app.config["SECURITY_TOTP_SECRETS"] = {
        "1": "TjQ9Qa31VOrfEzuPy4VHQWPCTmRzCnFzMKLxXYiZu9B"
    }
    app.config["SECURITY_FRESHNESS"] = datetime.timedelta(minutes=60)
    app.config["SECURITY_FRESHNESS_GRACE_PERIOD"] = datetime.timedelta(minutes=2)
    app.config["SECURITY_USERNAME_ENABLE"] = True

    class TestWebauthnUtil(WebauthnUtil):
        def generate_challenge(self, nbytes: t.Optional[int] = None) -> str:
            # Use a constant Challenge so we can us this app to generate gold
            # responses for use in unit testing. See test_webauthn.
            # NEVER NEVER NEVER do this in production
            return "smCCiy_k2CqQydSQ_kPEjV5a2d0ApfatcpQ1aXDmQPo"

    # Turn on all features (except passwordless since that removes normal login)
    for opt in [
        "changeable",
        "recoverable",
        "registerable",
        "trackable",
        "NOTpasswordless",
        "confirmable",
        "two_factor",
        "unified_signin",
        "webauthn",
    ]:
        app.config["SECURITY_" + opt.upper()] = True

    if os.environ.get("SETTINGS"):
        # Load settings from a file pointed to by SETTINGS
        app.config.from_envvar("SETTINGS")
    # Allow any SECURITY_ config to be set in environment.
    for ev in os.environ:
        if ev.startswith("SECURITY_") or ev.startswith("SQLALCHEMY_"):
            app.config[ev] = _find_bool(os.environ.get(ev))
    mail = FlashMail(app)
    app.mail = mail

    app.json_encoder = JSONEncoder

    # Create database models and hook up.
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db = SQLAlchemy(app)
    fsqla.FsModels.set_db_info(db)

    class Role(db.Model, fsqla.FsRoleMixin):
        pass

    class User(db.Model, fsqla.FsUserMixin):
        pass

    class WebAuthn(db.Model, fsqla.FsWebAuthnMixin):
        pass

    # Setup Flask-Security
    user_datastore = SQLAlchemyUserDatastore(db, User, Role, WebAuthn)
    security = Security(app, user_datastore, webauthn_util_cls=TestWebauthnUtil)

    try:
        import flask_babel

        babel = flask_babel.Babel(app)
    except ImportError:
        try:
            import flask_babelex

            babel = flask_babelex.Babel(app)
        except ImportError:
            babel = None

    if babel:

        @babel.localeselector
        def get_locale():
            # For a given session - set lang based on first request.
            # Honor explicit url request first
            if "lang" not in session:
                locale = request.args.get("lang", None)
                if not locale:
                    locale = request.accept_languages.best
                if not locale:
                    locale = "en"
                if locale:
                    session["lang"] = locale
            return session.get("lang", None).replace("-", "_")

    @app.before_first_request
    def clear_lang():
        session.pop("lang", None)

    # Create a user to test with
    @app.before_first_request
    def create_user():
        db.create_all()
        test_acct = "test@test.com"
        if not user_datastore.find_user(email=test_acct):
            add_user(user_datastore, test_acct, "password", ["admin"])
            print("Created User: {} with password {}".format(test_acct, "password"))

    @app.after_request
    def allow_absolute_redirect(r):
        # This is JUST to test odd possible redirects that look relative but are
        # interpreted by browsers as absolute.
        # DON'T SET THIS IN YOUR APPLICATION!
        r.autocorrect_location_header = False
        return r

    @user_registered.connect_via(app)
    def on_user_registered(myapp, user, confirm_token, **extra):
        flash(f"To confirm {user.email} - go to /confirm/{confirm_token}")

    @reset_password_instructions_sent.connect_via(app)
    def on_reset(myapp, user, token, **extra):
        flash(f"Go to /reset/{token}")

    @tf_security_token_sent.connect_via(app)
    def on_token_sent(myapp, user, token, method, **extra):
        flash(
            "User {} was sent two factor token {} via {}".format(
                user.calc_username(), token, method
            )
        )

    @us_security_token_sent.connect_via(app)
    def on_us_token_sent(myapp, user, token, method, **extra):
        flash(
            "User {} was sent sign in code {} via {}".format(
                user.calc_username(), token, method
            )
        )

    # Views
    @app.route("/")
    @login_required
    def home():
        return render_template_string(
            """
            {% include 'security/_messages.html' %}
            {{ _fsdomain('Welcome') }} {{email}} !
            {% include "security/_menu.html" %}
            """,
            email=current_user.email,
            security=security,
        )

    @app.route("/basicauth")
    @auth_required("basic")
    def basic():
        return render_template_string("Basic auth success")

    @app.route("/protected")
    @auth_required()
    def protected():
        return render_template_string("Protected endpoint")

    return app


def add_user(ds, email, password, roles):
    pw = hash_password(password)
    roles = [ds.find_or_create_role(rn) for rn in roles]
    ds.commit()
    user = ds.create_user(
        email=email, password=pw, active=True, confirmed_at=datetime.datetime.utcnow()
    )
    ds.commit()
    for role in roles:
        ds.add_role_to_user(user, role)
    ds.commit()


if __name__ == "__main__":
    create_app().run(port=5001)
