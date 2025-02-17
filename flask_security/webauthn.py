"""
    flask_security.webauthn
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    Flask-Security WebAuthn module

    :copyright: (c) 2021-2021 by J. Christopher Wagner (jwag).
    :license: MIT, see LICENSE for more details.

    This implements support for webauthn/FIDO2 Level 2 using the py_webauthn package.

    Check out: https://golb.hplar.ch/2019/08/webauthn.html
    for some ideas on recovery and adding additional authenticators.

    For testing - you can see your YubiKey (or other) resident keys in chrome!
    chrome://settings/securityKeys

    Observation: if key isn't resident than Chrome for example won't let you use
    it if it isn't part of allowedCredentials - throw error: referencing:
    https://www.w3.org/TR/webauthn-2/#sctn-privacy-considerations-client

    TODO:
        - deal with fs_webauthn_uniquifier for existing users
        - docs!
        - openapi.yml
        - add signals
        - integrate with unified signin?
        - make sure reset functions reset fs_webauthn - and remove credentials?
        - context processors
        - update/add examples to support webauthn
        - does remember me make sense?
        - should we store things like user verified in 'last use'...
        - config for allow as Primary, TF only,  allow as Primary/MFA
        - some options to request user verification and check on register - i.e.
          'I want a two-factor capable key'
        - Add a way to order registered credentials so we can return an ordered list
          in allowCredentials.
        - Deal with username and security implications
        - Research: by insisting on 2FA if user has registered a webauthn - things
          get interesting if they try to log in on a different device....
          How would they register a security key for a new device? They would need
          some OTHER 2FA? Force them to register a NEW webauthn key?

"""

import datetime
import json
import typing as t
from functools import partial

from flask import after_this_request, request
from flask_login import current_user
from werkzeug.datastructures import MultiDict
from wtforms import BooleanField, HiddenField, StringField, SubmitField

try:
    import webauthn
    from webauthn.authentication.verify_authentication_response import (
        VerifiedAuthentication,
    )
    from webauthn.registration.verify_registration_response import VerifiedRegistration
    from webauthn.helpers.exceptions import (
        InvalidAuthenticationResponse,
        InvalidRegistrationResponse,
    )
    from webauthn.helpers.structs import (
        AuthenticationCredential,
        AuthenticatorTransport,
        PublicKeyCredentialDescriptor,
        PublicKeyCredentialType,
        RegistrationCredential,
        UserVerificationRequirement,
    )
    from webauthn.helpers import bytes_to_base64url
except ImportError:  # pragma: no cover
    pass

from .decorators import anonymous_user_required, auth_required, unauth_csrf
from .forms import Form, Required, get_form_field_label
from .proxies import _security, _datastore
from .quart_compat import get_quart_status
from .utils import (
    base_render_json,
    check_and_get_token_status,
    config_value as cv,
    do_flash,
    find_user,
    json_error_response,
    get_message,
    get_post_login_redirect,
    get_within_delta,
    login_user,
    suppress_form_csrf,
    url_for_security,
    view_commit,
)

if t.TYPE_CHECKING:  # pragma: no cover
    from flask.typing import ResponseValue
    from .datastore import User, WebAuthn

if get_quart_status():  # pragma: no cover
    from quart import redirect
else:
    from flask import redirect


class WebAuthnRegisterForm(Form):

    name = StringField(
        get_form_field_label("credential_nickname"),
        validators=[Required(message="WEBAUTHN_NAME_REQUIRED")],
    )
    submit = SubmitField(label=get_form_field_label("submit"), id="wan_register")

    def validate(self):
        if not super().validate():
            return False
        inuse = any([self.name.data == cred.name for cred in current_user.webauthn])
        if inuse:
            msg = get_message("WEBAUTHN_NAME_INUSE", name=self.name.data)[0]
            self.name.errors.append(msg)
            return False
        return True


class WebAuthnRegisterResponseForm(Form):
    credential = HiddenField()
    submit = SubmitField(label=get_form_field_label("submit"))

    # from state
    challenge: str
    name: str
    # this is returned to caller (not part of the client form)
    registration_verification: "VerifiedRegistration"
    transports: t.List[str] = []
    extensions: str

    def validate(self) -> bool:
        if not super().validate():
            return False  # pragma: no cover
        inuse = any([self.name == cred.name for cred in current_user.webauthn])
        if inuse:
            msg = get_message("WEBAUTHN_NAME_INUSE", name=self.name)[0]
            self.credential.errors.append(msg)
            return False
        try:
            reg_cred = RegistrationCredential.parse_raw(self.credential.data)
        except ValueError:
            self.credential.errors.append(get_message("API_ERROR")[0])
            return False
        try:
            self.registration_verification = webauthn.verify_registration_response(
                credential=reg_cred,
                expected_challenge=self.challenge.encode(),
                expected_origin=_security._webauthn_util.origin(),
                expected_rp_id=request.host.split(":")[0],
                require_user_verification=True,
            )
            if _datastore.find_webauthn(credential_id=reg_cred.raw_id):
                msg = get_message("WEBAUTHN_CREDENTIAL_ID_INUSE")[0]
                self.credential.errors.append(msg)
                return False
        except InvalidRegistrationResponse as exc:
            self.credential.errors.append(
                get_message("WEBAUTHN_NO_VERIFY", cause=str(exc))[0]
            )
            return False

        # Alas py_webauthn doesn't support extensions nor transports yet
        response_full = json.loads(self.credential.data)
        # TODO - verify this is JSON (created with JSON.stringify)
        self.extensions = response_full.get("extensions", None)
        self.transports = (
            [tr for tr in response_full["transports"]]
            if response_full.get("transports", None)
            else []
        )
        return True


class WebAuthnSigninForm(Form):

    # Identity isn't required since if you have a resident key you don't require this.
    # However for non-resident keys, and to allow us to return keys that HAVE
    # been registered with this application - adding identity is very useful.
    # Look at
    # https://www.w3.org/TR/2021/REC-webauthn-2-20210408/#sctn-username-enumeration
    # for possible concerns.
    identity = StringField(get_form_field_label("identity"))
    remember = BooleanField(get_form_field_label("remember_me"))
    submit = SubmitField(label=get_form_field_label("submit"), id="wan_signin")

    user: t.Optional["User"] = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.remember.default = cv("DEFAULT_REMEMBER_ME")

    def validate(self):
        if not super().validate():
            return False  # pragma: no cover
        if self.identity.data:
            self.user = find_user(self.identity.data)
            if not self.user:
                self.identity.errors.append(get_message("US_SPECIFY_IDENTITY")[0])
                return False
            if not self.user.is_active:
                self.identity.errors.append(get_message("DISABLED_ACCOUNT")[0])
                return False
        return True


class WebAuthnSigninResponseForm(Form):
    submit = SubmitField(label=get_form_field_label("submit"))
    credential = HiddenField()

    # returned to caller
    authentication_verification: "VerifiedAuthentication"
    user: t.Optional["User"] = None
    cred: t.Optional["WebAuthn"] = None
    # Set to True if this authentication qualifies as 'multi-factor'
    mf_check: bool = False

    def validate(self) -> bool:
        if not super().validate():
            return False
        try:
            auth_cred = AuthenticationCredential.parse_raw(self.credential.data)
        except ValueError:
            self.credential.errors.append(get_message("API_ERROR")[0])
            return False

        # Look up credential Id (raw_id) and user.
        self.cred = _datastore.find_webauthn(credential_id=auth_cred.raw_id)
        if not self.cred:
            self.credential.errors.append(
                get_message("WEBAUTHN_UNKNOWN_CREDENTIAL_ID")[0]
            )
            return False
        # This shouldn't be able to happen if datastore properly cascades
        # delete
        self.user = _datastore.find_user_from_webauthn(self.cred)
        if not self.user:  # pragma: no cover
            self.credential.errors.append(
                get_message("WEBAUTHN_ORPHAN_CREDENTIAL_ID")[0]
            )
            return False

        verify = partial(
            webauthn.verify_authentication_response,
            credential=auth_cred,
            expected_challenge=self.challenge.encode(),
            expected_origin=_security._webauthn_util.origin(),
            expected_rp_id=request.host.split(":")[0],
            credential_public_key=self.cred.public_key,
            credential_current_sign_count=self.cred.sign_count,
        )
        try:
            self.authentication_verification = verify(require_user_verification=True)
            self.mf_check = True
        except InvalidAuthenticationResponse:
            try:
                self.authentication_verification = verify(
                    require_user_verification=False
                )
            except InvalidAuthenticationResponse as exc:
                self.credential.errors.append(
                    get_message("WEBAUTHN_NO_VERIFY", cause=str(exc))[0]
                )
                return False
        return True


class WebAuthnDeleteForm(Form):

    name = StringField(
        get_form_field_label("credential_nickname"),
        validators=[Required(message="WEBAUTHN_NAME_REQUIRED")],
    )
    submit = SubmitField(label=get_form_field_label("delete"))

    def validate(self) -> bool:
        if not super().validate():
            return False
        if not any([self.name.data == cred.name for cred in current_user.webauthn]):
            self.name.errors.append(
                get_message("WEBAUTHN_NAME_NOT_FOUND", name=self.name.data)[0]
            )
            return False
        return True


@auth_required(
    lambda: cv("API_ENABLED_METHODS"),
    within=lambda: cv("FRESHNESS"),
    grace=lambda: cv("FRESHNESS_GRACE_PERIOD"),
)
def webauthn_register() -> "ResponseValue":
    """Start Registration for an existing authenticated user

    Note that it requires a POST to start the registration and must send 'name'
    in. We check here that user hasn't already registered an authenticator with that
    name.
    Also - this requires that the user already be logged in - so we can provide info
    as part of the GET that could otherwise be considered leaking user info.
    """
    payload: t.Dict[str, t.Any]

    form_class: t.Type[WebAuthnRegisterForm] = _security.wan_register_form
    if request.is_json:
        if request.content_length:
            form = form_class(MultiDict(request.get_json()), meta=suppress_form_csrf())
        else:
            form = form_class(formdata=None, meta=suppress_form_csrf())
    else:
        form = form_class(meta=suppress_form_csrf())

    if form.validate_on_submit():
        challenge = _security._webauthn_util.generate_challenge(
            cv("WAN_CHALLENGE_BYTES")
        )
        state = {"challenge": challenge, "name": form.name.data}

        credential_options = webauthn.generate_registration_options(
            challenge=challenge.encode(),
            rp_name=cv("WAN_RP_NAME"),
            rp_id=request.host.split(":")[0],
            user_id=current_user.fs_webauthn_uniquifier,
            user_name=current_user.calc_username(),
            timeout=cv("WAN_REGISTER_TIMEOUT"),
            authenticator_selection=_security._webauthn_util.authenticator_selection(
                current_user
            ),
            exclude_credentials=create_credential_list(current_user),
        )
        #
        co_json = json.loads(webauthn.options_to_json(credential_options))
        co_json["extensions"] = {"credProps": True}

        state_token = _security.wan_serializer.dumps(state)

        if _security._want_json(request):
            payload = {
                "credential_options": co_json,
                "wan_state": state_token,
            }
            return base_render_json(form, include_user=False, additional=payload)

        return _security.render_template(
            cv("WAN_REGISTER_TEMPLATE"),
            wan_register_form=form,
            wan_register_response_form=WebAuthnRegisterResponseForm(),
            wan_state=state_token,
            credential_options=json.dumps(co_json),
        )

    current_creds = []
    for cred in current_user.webauthn:
        cl = {
            "name": cred.name,
            "credential_id": bytes_to_base64url(cred.credential_id),
            "transports": cred.transports.split(","),
            "lastuse": cred.lastuse_datetime.isoformat(),
        }
        # TODO: i18n
        discoverable = "Unknown"
        if cred.extensions:
            extensions = json.loads(cred.extensions)
            if "credProps" in extensions:
                discoverable = extensions["credProps"].get("rk", "Unknown")
        cl["discoverable"] = discoverable
        current_creds.append(cl)

    payload = {"registered_credentials": current_creds}
    if _security._want_json(request):
        return base_render_json(form, additional=payload)
    # TODO context processors
    return _security.render_template(
        cv("WAN_REGISTER_TEMPLATE"),
        wan_register_form=form,
        wan_delete_form=_security.wan_delete_form(),
        registered_credentials=current_creds,
    )


@auth_required(lambda: cv("API_ENABLED_METHODS"))
def webauthn_register_response(token: str) -> "ResponseValue":
    """Response from browser."""

    form_class: t.Type[
        WebAuthnRegisterResponseForm
    ] = _security.wan_register_response_form
    if request.is_json:
        form = form_class(MultiDict(request.get_json()), meta=suppress_form_csrf())
    else:
        form = form_class(meta=suppress_form_csrf())

    expired, invalid, state = check_and_get_token_status(
        token, "wan", get_within_delta("WAN_REGISTER_WITHIN")
    )
    if invalid:
        m, c = get_message("API_ERROR")
    if expired:
        m, c = get_message("WEBAUTHN_EXPIRED", within=cv("WAN_REGISTER_WITHIN"))
    if invalid or expired:
        if _security._want_json(request):
            payload = json_error_response(errors=m)
            return _security._render_json(payload, 400, None, None)
        do_flash(m, c)
        return redirect(url_for_security("wan_register"))

    form.challenge = state["challenge"]
    form.name = state["name"]
    if form.validate_on_submit():

        # store away successful registration
        after_this_request(view_commit)

        # convert transports to comma separated
        transports = ",".join(form.transports)

        _datastore.create_webauthn(
            current_user._get_current_object(),  # Not needed with Werkzeug >2.0.0
            name=state["name"],
            credential_id=form.registration_verification.credential_id,
            public_key=form.registration_verification.credential_public_key,
            sign_count=form.registration_verification.sign_count,
            transports=transports,
            extensions=form.extensions,
        )

        if _security._want_json(request):
            return base_render_json(form)
        msg, c = get_message("WEBAUTHN_REGISTER_SUCCESSFUL", name=state["name"])
        do_flash(msg, c)
        return redirect(url_for_security("wan_register"))

    if _security._want_json(request):
        return base_render_json(form)
    if len(form.errors) > 0:
        do_flash(form.errors["credential"][0], "error")
    return redirect(url_for_security("wan_register"))


@anonymous_user_required
@unauth_csrf(fall_through=True)
def webauthn_signin() -> "ResponseValue":
    form_class: t.Type[WebAuthnSigninForm] = _security.wan_signin_form
    if request.is_json:
        if request.content_length:
            form = form_class(MultiDict(request.get_json()), meta=suppress_form_csrf())
        else:
            form = form_class(formdata=None, meta=suppress_form_csrf())
    else:
        form = form_class(meta=suppress_form_csrf())

    if form.validate_on_submit():
        challenge = _security._webauthn_util.generate_challenge(
            cv("WAN_CHALLENGE_BYTES")
        )
        state = {
            "challenge": challenge,
        }

        # If they passed in an identity - look it up - if we find it we
        # can populate allowedCredentials.
        allow_credentials = None
        if form.user:
            allow_credentials = create_credential_list(form.user)

        options = webauthn.generate_authentication_options(
            rp_id=request.host.split(":")[0],
            challenge=challenge.encode(),
            timeout=cv("WAN_SIGNIN_TIMEOUT"),
            user_verification=UserVerificationRequirement.DISCOURAGED,
            allow_credentials=allow_credentials,
        )

        o_json = json.loads(webauthn.options_to_json(options))
        state_token = _security.wan_serializer.dumps(state)
        if _security._want_json(request):
            payload = {"credential_options": o_json, "wan_state": state_token}
            return base_render_json(form, include_user=False, additional=payload)

        return _security.render_template(
            cv("WAN_SIGNIN_TEMPLATE"),
            wan_signin_form=form,
            wan_signin_response_form=WebAuthnSigninResponseForm(),
            wan_state=state_token,
            credential_options=json.dumps(o_json),
        )

    if _security._want_json(request):
        return base_render_json(form)
    return _security.render_template(
        cv("WAN_SIGNIN_TEMPLATE"),
        wan_signin_form=form,
        wan_signin_response_form=WebAuthnSigninResponseForm(),
    )


@anonymous_user_required
@unauth_csrf(fall_through=True)
def webauthn_signin_response(token: str) -> "ResponseValue":
    form_class: t.Type[WebAuthnSigninResponseForm] = _security.wan_signin_response_form
    if request.is_json:
        form = form_class(MultiDict(request.get_json()), meta=suppress_form_csrf())
    else:
        form = form_class(meta=suppress_form_csrf())

    expired, invalid, state = check_and_get_token_status(
        token, "wan", get_within_delta("WAN_SIGNIN_WITHIN")
    )
    if invalid:
        m, c = get_message("API_ERROR")
    if expired:
        m, c = get_message("WEBAUTHN_EXPIRED", within=cv("WAN_SIGNIN_WITHIN"))
    if invalid or expired:
        if _security._want_json(request):
            payload = json_error_response(errors=m)
            return _security._render_json(payload, 400, None, None)
        do_flash(m, c)
        return redirect(url_for_security("wan_signin"))

    form.challenge = state["challenge"]

    if form.validate_on_submit():
        remember_me = form.remember.data if "remember" in form else None

        # update last use and sign count
        after_this_request(view_commit)
        form.cred.lastuse_datetime = datetime.datetime.utcnow()
        form.cred.sign_count = form.authentication_verification.new_sign_count
        _datastore.put(form.cred)

        # login user
        login_user(form.user, remember=remember_me, authn_via=["webauthn"])

        goto_url = get_post_login_redirect()
        if _security._want_json(request):
            # Tell caller where we would go if forms based - they can use it or
            # not.
            payload = {"post_login_url": goto_url}
            return base_render_json(form, include_auth_token=True, additional=payload)
        return redirect(goto_url)

    if _security._want_json(request):
        return base_render_json(form)

    # Here on validate error - since the response is auto submitted - we go back to
    # signin form - for now use flash.
    # TODO set into a special form element error?
    signin_form = _security.wan_signin_form()
    if form.credential.errors:
        do_flash(form.credential.errors[0], "error")
    return _security.render_template(
        cv("WAN_SIGNIN_TEMPLATE"), wan_signin_form=signin_form
    )


@auth_required(lambda: cv("API_ENABLED_METHODS"))
def webauthn_delete() -> "ResponseValue":
    """Deletes an existing registered credential."""

    form_class: t.Type[WebAuthnDeleteForm] = _security.wan_delete_form
    if request.is_json:
        form = form_class(MultiDict(request.get_json()), meta=suppress_form_csrf())
    else:
        form = form_class(meta=suppress_form_csrf())

    if form.validate_on_submit():
        # validate made sure form.name.data exists.
        cred = [c for c in current_user.webauthn if c.name == form.name.data][0]
        after_this_request(view_commit)
        _datastore.delete_webauthn(cred)
        if _security._want_json(request):
            return base_render_json(form)
        msg, c = get_message("WEBAUTHN_CREDENTIAL_DELETED", name=form.name.data)
        do_flash(msg, c)

    if _security._want_json(request):
        return base_render_json(form)
    # TODO flash something?
    return redirect(url_for_security("wan_register"))


def has_webauthn_tf(user: "User") -> bool:
    # Return True if have a WebAuthn key designated for second factor
    security_keys = getattr(user, "webauthn", None)
    if security_keys:
        if len(security_keys) > 0:
            return True
    return False


def create_credential_list(user: "User") -> t.List["PublicKeyCredentialDescriptor"]:
    cl = []

    for cred in user.webauthn:
        descriptor = PublicKeyCredentialDescriptor(
            type=PublicKeyCredentialType.PUBLIC_KEY, id=cred.credential_id
        )
        if cred.transports:
            tlist = cred.transports.split(",")
            transports = [AuthenticatorTransport(transport) for transport in tlist]
            descriptor.transports = transports
        # TODO order is important - figure out a way to add 'weight'
        cl.append(descriptor)

    return cl
