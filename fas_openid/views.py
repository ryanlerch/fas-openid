from flask import Flask, request, session, g, redirect, url_for, \
     abort, render_template, flash, Response

from model import FASOpenIDStore

from fas_openid import APP as app, FAS, log_debug, log_info, log_warning, log_error
from fas_openid.model import FASOpenIDStore

from flask_fas import fas_login_required

from fas_openid import openid_teams as teams
from fas_openid import openid_cla as cla

from time import time
from datetime import datetime

import openid
from openid.extensions import sreg
from openid.extensions import pape
from openid.server.server import Server as openid_server
from openid.server import server
from openid.consumer import discover

from urlparse import urljoin

from flaskext.babel import gettext as _

from uuid import uuid4 as uuid

# Possible AUTH results
AUTH_NOT_LOGGED_IN = 0
AUTH_TIMEOUT = 1
AUTH_INCORRECT_IDENTITY = 2
AUTH_OK = 3
AUTH_TRUST_ROOT_NOT_OK = 4
AUTH_TRUST_ROOT_ASK = 5
AUTH_TRUST_ROOT_CONFIG_NOT_OK = 6

CLA_GROUPS = { 'cla_click': cla.CLA_URI_FEDORA_CLICK
             , 'cla_dell': cla.CLA_URI_FEDORA_DELL
             , 'cla_done': cla.CLA_URI_FEDORA_DONE
             , 'cla_fedora': cla.CLA_URI_FEDORA_FEDORA
             , 'cla_fpca': cla.CLA_URI_FEDORA_FPCA
             , 'cla_ibm': cla.CLA_URI_FEDORA_IBM
             , 'cla_intel': cla.CLA_URI_FEDORA_INTEL
             , 'cla_redhat': cla.CLA_URI_FEDORA_REDHAT
             }


def filter_cla_groups(groups):
    return [group for group in groups if not group in CLA_GROUPS.keys()]

def get_cla_uris(groups):
    return [CLA_GROUPS[group] for group in groups if group in CLA_GROUPS.keys()]


def get_server():
    if not hasattr(g, 'openid_server'):
        g.openid_server = openid_server(FASOpenIDStore(), op_endpoint=app.config['OPENID_ENDPOINT'])
    return g.openid_server

def complete_url_for(func, **values):
    return urljoin(app.config['OPENID_ENDPOINT'], url_for(func, **values))

def get_claimed_id(username):
    # The urljoin is so that we alway get <id>/ instead of both <id> and <id>/
    return urljoin(app.config['OPENID_IDENTITY_URL'] % username, '/')

def getPapeRequestInfo(request):
    pape_req = pape.Request.fromOpenIDRequest(request)
    if pape_req is None:
        return 0, [], {}
    return pape_req.max_auth_age, pape_req.preferred_auth_policies, pape_req.preferred_auth_level_types

def addSReg(request, response, user):
    sreg_req = sreg.SRegRequest.fromOpenIDRequest(request)
    sreg_data = { 'nickname'    : user.username
                , 'email'       : user.email
                , 'fullname'    : user.human_name
                , 'timezone'    : user.timezone
                }
    sreg_resp = sreg.SRegResponse.extractResponse(sreg_req, sreg_data)
    response.addExtension(sreg_resp)
    return sreg_resp.data

def addPape(request, response):
    done_yubikey = False

    auth_time = datetime.utcfromtimestamp(session['last_auth_time']).strftime('%Y-%m-%dT%H:%M:%SZ')
    auth_policies = []
    auth_levels = {}
    if done_yubikey:
        auth_policies.append(pape.AUTH_MULTI_FACTOR)
        auth_policies.append(pape.AUTH_MULTI_FACTOR_PHYSICAL)
        auth_policies.append(pape.AUTH_PHISHING_RESISTANT)
        auth_levels[pape.LEVELS_NIST] = 3
    else:
        auth_policies.append(pape.AUTH_NONE)
        auth_levels[pape.LEVELS_NIST] = 2
    pape_resp = pape.Response(auth_policies=auth_policies, auth_time=auth_time, auth_levels=auth_levels)
    response.addExtension(pape_resp)
    return auth_levels[pape.LEVELS_NIST]

def addTeams(request, response, groups):
    teams_req = teams.TeamsRequest.fromOpenIDRequest(request)
    teams_resp = teams.TeamsResponse.extractResponse(teams_req, groups)
    response.addExtension(teams_resp)
    return teams_resp.teams

def addCLAs(request, response, cla_uris):
    cla_req = cla.CLARequest.fromOpenIDRequest(request)
    cla_resp = cla.CLAResponse.extractResponse(cla_req, cla_uris)
    response.addExtension(cla_resp)
    return cla_resp.clas

def addToSessionArray(array, value):
    if array in session:
        session[array].append(value)
        session.modified = True
    else:
        session[array] = [value]

def getSessionValue(key, default_value=None):
    if key in session:
        return session[key]
    else:
        return default_value

def user_ask_trust_root(openid_request):
    if request.method == 'POST' and 'form_filled' in request.form:
        if not 'csrf_id' in session or not 'csrf_value' in request.form or request.form['csrf_value'] != session['csrf_id']:
            return 'CSRF Protection value invalid'
        if 'decided_allow' in request.form:
            addToSessionArray('TRUSTED_ROOTS', openid_request.trust_root)
        else:
            addToSessionArray('NON_TRUSTED_ROOTS', openid_request.trust_root)
        return redirect(request.url)
    session['csrf_id'] = uuid().hex
    # Get which stuff we will send
    sreg_data = { 'nickname'    : g.fas_user.username
                , 'email'       : g.fas_user.email
                , 'fullname'    : g.fas_user.human_name
                , 'timezone'    : g.fas_user.timezone
                }
    sreg_req = sreg.SRegRequest.fromOpenIDRequest(openid_request)
    sreg_resp = sreg.SRegResponse.extractResponse(sreg_req, sreg_data)
    teams_req = teams.TeamsRequest.fromOpenIDRequest(openid_request)
    teams_resp = teams.TeamsResponse.extractResponse(teams_req, filter_cla_groups(g.fas_user.groups))
    clas_req = cla.CLARequest.fromOpenIDRequest(openid_request)
    clas_resp = cla.CLAResponse.extractResponse(clas_req, get_cla_uris(g.fas_user.groups))
    # Show form
    return render_template('user_ask_trust_root.html'
                          , action              = request.url
                          , trust_root          = openid_request.trust_root
                          , sreg_policy_url     = sreg_req.policy_url
                          , sreg_data           = sreg_resp.data
                          , teams_provided      = teams_resp.teams
                          , cla_done            = cla.CLA_URI_FEDORA_DONE in clas_resp.clas
                          , csrf                = session['csrf_id']
                          )

@app.route('/robots.txt')
def view_robots():
    return 'User-Agent: *\nDisallow: /'

@app.route('/', methods=['GET', 'POST'])
def view_main():
    if 'openid.mode' in request.values:
        values = request.values
        session['values'] = request.values
        session.modified = True
    else:
        if 'values' in session:
            values = session['values']
        else:
            values = {}

    try:
        openid_request = get_server().decodeRequest(values)
    except server.ProtocolError, openid_error:
        return openid_respond(openid_error)

    if openid_request is None:
        return render_template('index.html', yadis_url=complete_url_for('view_yadis')), 200, {'X-XRDS-Location': complete_url_for('view_yadis')}
    elif openid_request.mode in ['checkid_immediate', 'checkid_setup']:
        authed = isAuthorized(openid_request)
        if authed == AUTH_OK:
            openid_response = openid_request.answer(True, identity=get_claimed_id(g.fas_user.username), claimed_id=get_claimed_id(g.fas_user.username))
            sreg_info = addSReg(openid_request, openid_response, g.fas_user)
            teams_info = addTeams(openid_request, openid_response, filter_cla_groups(g.fas_user.groups))
            cla_info = addCLAs(openid_request, openid_response, get_cla_uris(g.fas_user.groups))
            auth_level = addPape(openid_request, openid_response)
            log_info('Success', {'claimed_id': get_claimed_id(g.fas_user.username), 'trust_root': openid_request.trust_root, 'security_level': auth_level, 'message': 'The user succesfully claimed the identity'})
            log_debug('Info', {'teams': teams_info})
            return openid_respond(openid_response)
        elif authed == AUTH_TRUST_ROOT_ASK:
            # User needs to confirm trust root
            return user_ask_trust_root(openid_request)
        elif authed == AUTH_TRUST_ROOT_NOT_OK:
            log_info('Info', {'trust_root': openid_request.trust_root, 'message': 'User chose not to trust trust_root'})
            return openid_respond(openid_request.answer(False))
        elif authed == AUTH_TRUST_ROOT_CONFIG_NOT_OK:
            log_info('Info', {'trust_root': openid_request.trust_root, 'message': 'Configuration blacklisted this trust_root'})
            return openid_respond(openid_request.answer(False))
        elif openid_request.immediate:
            log_warning('Error', {'trust_root': openid_request.trust_root, 'message': 'Trust root demanded checkid_immediate'})
            return openid_respond(openid_request.answer(False))
        elif authed == AUTH_TIMEOUT:
            session['timeout'] = True
            session['next'] = request.base_url
            return redirect(app.config['LOGIN_URL'])
        elif g.fas_user is None:
            session['next'] = request.base_url
            session['trust_root'] = openid_request.trust_root
            return redirect(app.config['LOGIN_URL'])
        else:
            log_error('Failure', {'username': g.fas_user.username, 'attempted_claimed_id': openid_request.identity, 'trust_root': openid_request.trust_root, 'message': 'The user tried to claim an ID that is not theirs'})
            return 'This is not your ID! If it is, please contact the administrators at admin@fedoraproject.org. Be sure to mention your session ID: %(logid)s' % {'logid': session['log_id']}
    else:
        return openid_respond(get_server().handleRequest(openid_request))

def isAuthorized(openid_request):
    pape_req_time, pape_auth_policies, pape_auth_level_types = getPapeRequestInfo(openid_request)

    if g.fas_user is None:
        return AUTH_NOT_LOGGED_IN
    elif (pape_req_time) and (pape_req_time != 0) and (session['last_auth_time'] < (time() - pape_req_time)):
        return AUTH_TIMEOUT
    elif (app.config['MAX_AUTH_TIME'] != 0) and (session['last_auth_time'] < (time() - (app.config['MAX_AUTH_TIME'] * 60))):
        return AUTH_TIMEOUT
    # Add checks if yubikey is required by application
    elif (not openid_request.idSelect()) and (openid_request.identity != get_claimed_id(g.fas_user.username)):
        print 'Incorrect claimed id. Claimed: %s, correct: %s' % (openid_request.identity, get_claimed_id(g.fas_user.username))
        return AUTH_INCORRECT_IDENTITY
    elif openid_request.trust_root in app.config['TRUSTED_ROOTS']:
        return AUTH_OK
    elif openid_request.trust_root in app.config['NON_TRUSTED_ROOTS']:
        return AUTH_TRUST_ROOT_CONFIG_NOT_OK
    elif openid_request.trust_root in getSessionValue('TRUSTED_ROOTS', []):
        return AUTH_OK
    elif openid_request.trust_root in getSessionValue('NON_TRUSTED_ROOTS', []):
        return AUTH_TRUST_ROOT_NOT_OK
    else:
        # The user still needs to determine if he/she allows this trust root
        return AUTH_TRUST_ROOT_ASK

@app.route('/id/<username>/')
def view_id(username):
    return render_template('user.html', username=username, claimed_id=get_claimed_id(username), yadis_url=complete_url_for('view_yadis_id', username=username)), 200, {'X-XRDS-Location': complete_url_for('view_yadis_id', username=username)}


@app.route('/yadis/<username>.xrds')
def view_yadis_id(username):
    return Response(render_template('yadis_user.xrds', claimed_id=get_claimed_id(username), mimetype='application/xrds+xml'))

@app.route('/yadis.xrds')
def view_yadis():
    return Response(render_template('yadis.xrds', mimetype='application/xrds+xml'))

def openid_respond(openid_response):
    if 'values' in session:
        session['values'] = None
        session.modified = True
    try:
        webresponse = get_server().encodeResponse(openid_response)
        return (webresponse.body, webresponse.code, webresponse.headers)
    except server.EncodingError, why:
        headers = {'Content-type': 'text/plain; charset=UTF-8'} 
        return why.response.encodeToKVForm(), 400, headers


@app.route('/logout/')
def auth_logout():
    if not g.fas_user:
        return redirect(url_for('view_main'))
    FAS.logout()
    session.clear()
    flash(_('You have been logged out'))
    return redirect(url_for('view_main'))

@app.route('/login/', methods=['GET','POST'])
def auth_login():
    if not 'next' in request.args and not 'next' in session:
        return redirect(url_for('view_main'))
    if 'next' in request.args:
        session['next'] = request.args['next']
    if g.fas_user and not ('timeout' in session and session['timeout']): # We can also have "timeout" as of 0.4.0, indicating PAPE or application configuration requires a re-auth
        log_debug('Info', {'message': 'User tried to login but is already authenticated'})
        return redirect(session['next'])
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        if (not app.config['AVAILABLE_FILTER']) or (username in app.config['AVAILABLE_TO']):
            if FAS.login(username, password):
                log_info('Success', {'username': username, 'message': 'User authenticated succesfully'})
                session['last_auth_time'] = time()
                session['timeout'] = False
                session['trust_root'] = ''
                session.modified = True
                return redirect(session['next'])
            else:
                log_warning('Failure', {'username': username, 'message': 'User entered incorrect username or password'})
                flash(_('Incorrect username or password'))
        else:
            log_warning('Failure', {'username': username, 'message': 'Tried to login with an account that is not allowed to use this service'})
            flash(_('This service is limited to the following users: %(users)s', users=', '.join(app.config['AVAILABLE_TO'])))
    return render_template('login.html', trust_root=session['trust_root'])

@app.route('/test/')
@fas_login_required
def view_test():
    return render_template('test.html', user='%s' % g.fas_user)
