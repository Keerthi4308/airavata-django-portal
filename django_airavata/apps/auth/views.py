import logging
from urllib.parse import quote

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.core.exceptions import ObjectDoesNotExist
from django.core.mail import EmailMessage
from django.forms import ValidationError
from django.http import HttpResponseBadRequest
from django.http.request import split_domain_port
from django.shortcuts import redirect, render, resolve_url
from django.template import Context, Template
from django.urls import reverse
from requests_oauthlib import OAuth2Session

from . import forms, iam_admin_client, models

logger = logging.getLogger(__name__)


def start_login(request):
    return render(request, 'django_airavata_auth/login.html', {
        'next': request.GET.get('next', None),
        'options': settings.AUTHENTICATION_OPTIONS,
    })


def start_username_password_login(request):
    # return bad request if password isn't a configured option
    if 'password' not in settings.AUTHENTICATION_OPTIONS:
        return HttpResponseBadRequest("Username/password login is not enabled")
    return render(request,
                  'django_airavata_auth/login_username_password.html',
                  {
                      'next': request.GET.get('next', None),
                      'options': settings.AUTHENTICATION_OPTIONS,
                      'login_type': 'password'
                  })


def redirect_login(request, idp_alias):
    _validate_idp_alias(idp_alias)
    client_id = settings.KEYCLOAK_CLIENT_ID
    base_authorize_url = settings.KEYCLOAK_AUTHORIZE_URL
    redirect_uri = request.build_absolute_uri(
        reverse('django_airavata_auth:callback'))
    redirect_uri += '?idp_alias=' + quote(idp_alias)
    if 'next' in request.GET:
        redirect_uri += "&next=" + quote(request.GET['next'])
    oauth2_session = OAuth2Session(
        client_id, scope='openid', redirect_uri=redirect_uri)
    authorization_url, state = oauth2_session.authorization_url(
        base_authorize_url)
    authorization_url += '&kc_idp_hint=' + quote(idp_alias)
    # Store state in session for later validation (see backends.py)
    request.session['OAUTH2_STATE'] = state
    request.session['OAUTH2_REDIRECT_URI'] = redirect_uri
    return redirect(authorization_url)


def _validate_idp_alias(idp_alias):
    external_auth_options = settings.AUTHENTICATION_OPTIONS['external']
    valid_idp_aliases = [ext['idp_alias'] for ext in external_auth_options]
    if idp_alias not in valid_idp_aliases:
        raise Exception("idp_alias is not valid")


def handle_login(request):
    username = request.POST['username']
    password = request.POST['password']
    login_type = request.POST.get('login_type', None)
    template = "django_airavata_auth/login.html"
    if login_type and login_type == 'password':
        template = "django_airavata_auth/login_username_password.html"
    user = authenticate(username=username, password=password, request=request)
    logger.debug("authenticated user: {}".format(user))
    try:
        if user is not None:
            login(request, user)
            next_url = request.POST.get('next', settings.LOGIN_REDIRECT_URL)
            return redirect(next_url)
        else:
            messages.error(request, "Login failed. Please try again.")
    except Exception as err:
        messages.error(request,
                       "Login failed: {}. Please try again.".format(str(err)))
    return render(request, template, {
        'username': username,
        'next': request.POST.get('next', None),
        'options': settings.AUTHENTICATION_OPTIONS,
        'login_type': login_type,
    })


def start_logout(request):
    logout(request)
    redirect_url = request.build_absolute_uri(
        resolve_url(settings.LOGOUT_REDIRECT_URL))
    return redirect(settings.KEYCLOAK_LOGOUT_URL +
                    "?redirect_uri=" + quote(redirect_url))


def callback(request):
    try:
        user = authenticate(request=request)
        login(request, user)
        next_url = request.GET.get('next', settings.LOGIN_REDIRECT_URL)
        return redirect(next_url)
    except Exception as err:
        logger.exception("An error occurred while processing OAuth2 "
                         "callback: {}".format(request.build_absolute_uri()))
        messages.error(
            request,
            "Failed to process OAuth2 callback: {}".format(str(err)))
        idp_alias = request.GET.get('idp_alias')
        return redirect(reverse('django_airavata_auth:callback-error',
                                args=(idp_alias,)))


def callback_error(request, idp_alias):
    _validate_idp_alias(idp_alias)
    # Create a filtered options object with just the given idp_alias
    options = {
        'external': []
    }
    for ext in settings.AUTHENTICATION_OPTIONS['external']:
        if ext['idp_alias'] == idp_alias:
            options['external'].append(ext.copy())

    return render(request, 'django_airavata_auth/callback-error.html', {
        'idp_alias': idp_alias,
        'options': options,
    })


def create_account(request):
    if request.method == 'POST':
        form = forms.CreateAccountForm(request.POST)
        if form.is_valid():
            try:
                username = form.cleaned_data['username']
                email = form.cleaned_data['email']
                first_name = form.cleaned_data['first_name']
                last_name = form.cleaned_data['last_name']
                password = form.cleaned_data['password']
                success = iam_admin_client.register_user(
                    username, email, first_name, last_name, password)
                if not success:
                    form.add_error(None, ValidationError(
                        "Failed to register user with IAM service"))
                else:
                    _create_and_send_email_verification_link(
                        request, username, email, first_name, last_name)
                    messages.success(
                        request,
                        "Account request processed successfully. Before you "
                        "can login you need to confirm your email address. "
                        "We've sent you an email with a link that you should "
                        "click on to complete the account creation process.")
                    return redirect(
                        reverse('django_airavata_auth:create_account'))
            except Exception as e:
                logger.exception(
                    "Failed to create account for user", exc_info=e)
                form.add_error(None, ValidationError(e.message))
    else:
        form = forms.CreateAccountForm()
    return render(request, 'django_airavata_auth/create_account.html', {
        'options': settings.AUTHENTICATION_OPTIONS,
        'form': form
    })


def verify_email(request, code):

    try:
        email_verification = models.EmailVerification.objects.get(
            verification_code=code)
        email_verification.verified = True
        email_verification.save()
        # Check if user is enabled, if so redirect to login page
        username = email_verification.username
        logger.debug("Email address verified for {}".format(username))
        if iam_admin_client.is_user_enabled(username):
            logger.debug("User {} is already enabled".format(username))
            messages.success(
                request,
                "Your account has already been successfully created. "
                "Please log in now.")
            return redirect(reverse('django_airavata_auth:login'))
        else:
            logger.debug("Enabling user {}".format(username))
            # enable user and inform admins
            iam_admin_client.enable_user(username)
            user_profile = iam_admin_client.get_user(username)
            new_user_email_template = models.EmailTemplate.objects.get(
                pk=models.NEW_USER_EMAIL_TEMPLATE)
            email_address = user_profile.emails[0]
            first_name = user_profile.firstName
            last_name = user_profile.lastName
            domain, port = split_domain_port(request.get_host())
            context = Context({
                "username": username,
                "email": email_address,
                "first_name": first_name,
                "last_name": last_name,
                "portal_title": settings.PORTAL_TITLE,
                "gateway_id": settings.GATEWAY_ID,
                "http_host": domain,
            })
            subject = Template(new_user_email_template.subject).render(context)
            body = Template(new_user_email_template.body).render(context)
            msg = EmailMessage(subject=subject,
                               body=body,
                               from_email="{} <{}>".format(
                                   settings.PORTAL_TITLE,
                                   settings.SERVER_EMAIL),
                               to=[a[1] for a in settings.ADMINS])
            msg.content_subtype = 'html'
            msg.send()
            messages.success(
                request,
                "Your account has been successfully created. "
                "Please log in now.")
            return redirect(reverse('django_airavata_auth:login'))
    except ObjectDoesNotExist as e:
        # if doesn't exist, give user a form where they can enter their
        # username to resend verification code
        logger.exception("EmailVerification object doesn't exist for "
                         "code {}".format(code))
        messages.error(
            request,
            "Email verification failed. Please enter your username and we "
            "will send you another email verification link.")
        return redirect(reverse('django_airavata_auth:resend_email_link'))
    except Exception as e:
        logger.exception("Email verification processing failed!")
        messages.error(
            request,
            "Email verification failed. Please try clicking the email "
            "verification link again later.")
        return redirect(reverse('django_airavata_auth:create_account'))


def resend_email_link(request):

    if request.method == 'POST':
        form = forms.ResendEmailVerificationLinkForm(request.POST)
        if form.is_valid():
            try:
                username = form.cleaned_data['username']
                if iam_admin_client.is_user_exist(username):
                    user_profile = iam_admin_client.get_user(username)
                    email_address = user_profile.emails[0]
                    _create_and_send_email_verification_link(
                        request,
                        username,
                        email_address,
                        user_profile.firstName,
                        user_profile.lastName)
                    messages.success(
                        request,
                        "Email verification link sent successfully. Please "
                        "click on the link in the email that we sent "
                        "to your email address.")
                else:
                    messages.error(
                        request,
                        "Unable to resend email verification link. Please "
                        "contact the website administrator for further "
                        "assistance.")
                return redirect(
                    reverse('django_airavata_auth:resend_email_link'))
            except Exception as e:
                logger.exception(
                    "Failed to resend email verification link", exc_info=e)
                form.add_error(None, ValidationError(str(e)))
    else:
        form = forms.ResendEmailVerificationLinkForm()
    return render(request, 'django_airavata_auth/verify_email.html', {
        'form': form
    })


def _create_and_send_email_verification_link(
        request, username, email, first_name, last_name):

    email_verification = models.EmailVerification(
        username=username)
    email_verification.save()

    verification_uri = request.build_absolute_uri(
        reverse(
            'django_airavata_auth:verify_email', kwargs={
                'code': email_verification.verification_code}))
    logger.debug(
        "verification_uri={}".format(verification_uri))

    verify_email_template = models.EmailTemplate.objects.get(
        pk=models.VERIFY_EMAIL_TEMPLATE)
    context = Context({
        "username": username,
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "portal_title": settings.PORTAL_TITLE,
        "url": verification_uri,
    })
    subject = Template(verify_email_template.subject).render(context)
    body = Template(verify_email_template.body).render(context)
    msg = EmailMessage(subject=subject, body=body,
                       from_email="{} <{}>".format(
                           settings.PORTAL_TITLE, settings.SERVER_EMAIL),
                       to=["{} {} <{}>".format(first_name, last_name, email)])
    msg.content_subtype = 'html'
    msg.send()
