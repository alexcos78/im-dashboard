from app import app, oidc_blueprint, settings, utils, appdb, cred
from oauthlib.oauth2.rfc6749.errors import InvalidTokenError, TokenExpiredError
from werkzeug.exceptions import Forbidden
from flask import json, render_template, request, redirect, url_for, flash, session, Markup
import requests, json
import yaml
import io, os, sys
from functools import wraps
from urllib.parse import urlparse
from radl import radl_parse
from radl.radl import deploy

app.jinja_env.filters['tojson_pretty'] = utils.to_pretty_json

toscaTemplates = utils.loadToscaTemplates(settings.toscaDir)
toscaInfo = utils.extractToscaInfo(settings.toscaDir,settings.toscaParamsDir,toscaTemplates)

app.logger.debug("TOSCA INFO: " + json.dumps(toscaInfo))


@app.before_request
def before_request_checks():
    if 'external_links' not in session:
       session['external_links'] = settings.external_links

def authorized_with_valid_token(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):

        try:
            if not oidc_blueprint.session.authorized or 'username' not in session:
                return redirect(url_for('login'))

            if oidc_blueprint.session.token['expires_in'] < 20:
                app.logger.debug("Force refresh token")
                oidc_blueprint.session.get('/userinfo')
        except (InvalidTokenError, TokenExpiredError) as e:
            flash("Token expired.", 'warning')
            return redirect(url_for('login'))

        return f(*args, **kwargs)

    return decorated_function

@app.route('/settings')
@authorized_with_valid_token
def show_settings():
    return render_template('settings.html', oidc_url=settings.oidcUrl, im_url=settings.imUrl)

@app.route('/login')
def login():
    session.clear()
    return render_template('home.html')

@app.route('/')
def home():
    if not oidc_blueprint.session.authorized:
        return redirect(url_for('login'))

    try:
        account_info = oidc_blueprint.session.get(urlparse(settings.oidcUrl)[2] + "/userinfo")
    except (InvalidTokenError, TokenExpiredError) as e:
        flash("Token expired.", 'warning')
        return redirect(url_for('login'))

    if account_info.ok:
        account_info_json = account_info.json()

        session["vos"] = None
        if 'eduperson_entitlement' in account_info_json:
            session["vos"] = utils.getUserVOs(account_info_json['eduperson_entitlement'])

        if settings.oidcGroups:
            user_groups = []
            if 'groups' in account_info_json:
                user_groups = account_info_json['groups']
            elif 'eduperson_entitlement' in account_info_json:
                user_groups = account_info_json['eduperson_entitlement']
            if not set(settings.oidcGroups).issubset(user_groups):
                app.logger.debug("No match on group membership. User group membership: " + json.dumps(user_groups))
                message = Markup('You need to be a member of the following groups: {0}. <br> Please, visit <a href="{1}">{1}</a> and apply for the requested membership.'.format(json.dumps(settings.oidcGroups), settings.oidcUrl))
                raise Forbidden(description=message)

        session['userid'] = account_info_json['sub']
        session['username'] = account_info_json['name']
        if 'email' in account_info_json:
            session['gravatar'] = utils.avatar(account_info_json['email'], 26)
        else:
            session['gravatar'] = utils.avatar(account_info_json['sub'], 26)
        access_token = oidc_blueprint.token['access_token']

        return render_template('portfolio.html', templates=toscaInfo)
    else:
        flash("Error getting User info: \n" + account_info.text, 'error')
        return render_template('home.html')


@app.route('/vminfo/<infid>/<vmid>')
@authorized_with_valid_token
def showvminfo(infid=None, vmid=None):

    access_token = oidc_blueprint.session.token['access_token']

    auth_data = utils.getUserAuthData(access_token)
    headers = {"Authorization": auth_data, "Accept": "application/json"}

    url = "%s/infrastructures/%s/vms/%s" % (settings.imUrl, infid, vmid)
    response = requests.get(url, headers=headers)

    vminfo = {}
    state = ""
    nets = ""
    deployment = ""
    if not response.ok:
        flash("Error retrieving VM info: \n" + response.text, 'error')
    else:
        app.logger.debug("VM Info: %s" % response.text)
        vminfo = utils.format_json_radl(response.json()["radl"])
        if "cpu.arch" in vminfo:
            del vminfo["cpu.arch"]
        if "state" in vminfo:
            state = vminfo["state"]
            del vminfo["state"]
        if "provider.type" in vminfo:
            deployment = vminfo["provider.type"]
            del vminfo["provider.type"]
        if "provider.host" in vminfo:
            if "provider.port" in vminfo:
                deployment += ": %s:%s" % (vminfo["provider.host"], vminfo["provider.port"])
                del vminfo["provider.port"]
            else:
                deployment += ": " + vminfo["provider.host"]
            del vminfo["provider.host"]

        cont = 0
        while "net_interface.%s.ip" % cont in vminfo:
            if cont > 0:
                nets += Markup('<br/>')
            nets += Markup('<i class="fa fa-network-wired"></i>')
            nets += " %s: %s" % (cont, vminfo["net_interface.%s.ip" % cont])
            del vminfo["net_interface.%s.ip" % cont]
            cont += 1

        cont = 0
        while "net_interface.%s.connection" % cont in vminfo:
              del vminfo["net_interface.%s.connection" % cont]
              cont += 1

        for elem in vminfo:
            if elem.endswith("size") and isinstance(vminfo[elem], int):
                vminfo[elem] = "%d GB" % (vminfo[elem]/1073741824)

    return render_template('vminfo.html', infid=infid, vmid=vmid, vminfo=vminfo, state=state, nets=nets, deployment=deployment)


@app.route('/managevm/<op>/<infid>/<vmid>')
@authorized_with_valid_token
def managevm(op=None, infid=None, vmid=None):

    access_token = oidc_blueprint.session.token['access_token']

    auth_data = utils.getUserAuthData(access_token)
    headers = {"Authorization": auth_data, "Accept": "application/json"}

    op = op.lower()
    if op in ["stop", "start", "reboot"]:
        url = "%s/infrastructures/%s/vms/%s/%s" % (settings.imUrl, infid, vmid, op)
        response = requests.put(url, headers=headers)
    elif op == "terminate":
        url = "%s/infrastructures/%s/vms/%s" % (settings.imUrl, infid, vmid)
        response = requests.delete(url, headers=headers)

    if response.ok:
        flash("Operation '%s' successfully made on VM ID: %s" % (op, vmid), 'info')
    else:
        flash("Error making %s op on VM %s: \n%s" % (op, vmid, response.text), 'error')

    if op == "terminate":
        return redirect(url_for('showinfrastructures'))
    else:
        return redirect(url_for('showvminfo', infid=infid, vmid=vmid))

@app.route('/infrastructures')
@authorized_with_valid_token
def showinfrastructures():

    access_token = oidc_blueprint.session.token['access_token']

    auth_data = utils.getUserAuthData(access_token)
    headers = {"Authorization": auth_data, "Accept": "application/json"}

    url = "%s/infrastructures" % settings.imUrl
    response = requests.get(url, headers=headers)

    infrastructures = {}
    if not response.ok:
        flash("Error retrieving infrastructure list: \n" + response.text, 'error')
    else:
        app.logger.debug("Infrastructures: %s" % response.text)
        state_res = response.json()
        if "uri-list" in state_res:
            inf_id_list = [elem["uri"] for elem in state_res["uri-list"]]
        else:
            inf_id_list = []
        for inf_id in inf_id_list:
            url = "%s/state" % inf_id
            response = requests.get(url, headers=headers)
            if not response.ok:
                flash("Error retrieving infrastructure %s state: \n%s" % (inf_id, response.text), 'error')
            else:
                inf_state = response.json()
                infrastructures[os.path.basename(inf_id)] = inf_state['state']

    return render_template('infrastructures.html', infrastructures=infrastructures)


@app.route('/reconfigure/<infid>')
@authorized_with_valid_token
def infreconfigure(infid=None):

    access_token = oidc_blueprint.session.token['access_token']
    auth_data = utils.getUserAuthData(access_token)
    headers = {"Authorization": auth_data}

    url = "%s/infrastructures/%s/reconfigure" % (settings.imUrl, infid)
    response = requests.put(url, headers=headers)

    if response.ok:
        flash("Infrastructure successfuly reconfigured.", "info")
    else:
        flash("Error reconfiguring Infrastructure: \n" + response.text, "error")

    return redirect(url_for('showinfrastructures'))


@app.route('/template/<infid>')
@authorized_with_valid_token
def template(infid=None):

    access_token = oidc_blueprint.session.token['access_token']
    auth_data = utils.getUserAuthData(access_token)
    headers = {"Authorization": auth_data}

    url = "%s/infrastructures/%s/tosca" % (settings.imUrl, infid)
    response = requests.get(url, headers=headers)

    if not response.ok:
        flash("Error getting template: \n" + response.text, "error")
        template = ""
    else:
        template = response.text
    return render_template('deptemplate.html', template=template)


@app.route('/log/<infid>')
@authorized_with_valid_token
def inflog(infid=None):

    access_token = oidc_blueprint.session.token['access_token']
    auth_data = utils.getUserAuthData(access_token)
    headers = {"Authorization": auth_data}

    url = "%s/infrastructures/%s/contmsg" % (settings.imUrl, infid)
    response = requests.get(url, headers=headers, verify=False)

    if not response.ok:
      log="Not found"
    else:
      log = response.text
    return render_template('inflog.html', log=log)

@app.route('/vmlog/<infid>/<vmid>')
@authorized_with_valid_token
def vmlog(infid=None, vmid=None):

    access_token = oidc_blueprint.session.token['access_token']
    auth_data = utils.getUserAuthData(access_token)
    headers = {"Authorization": auth_data}

    url = "%s/infrastructures/%s/vms/%s/contmsg" % (settings.imUrl, infid, vmid)
    response = requests.get(url, headers=headers, verify=False)

    if not response.ok:
      log="Not found"
    else:
      log = response.text
    return render_template('inflog.html', log=log, vmid=vmid)

@app.route('/outputs/<infid>')
@authorized_with_valid_token
def infoutputs(infid=None):

    access_token = oidc_blueprint.session.token['access_token']
    auth_data = utils.getUserAuthData(access_token)
    headers = {"Authorization": auth_data}

    url = "%s/infrastructures/%s/outputs" % (settings.imUrl, infid)
    response = requests.get(url, headers=headers, verify=False)

    if not response.ok:
      outputs = {}
    else:
      outputs = response.json()["outputs"]
      for elem in outputs:
          if isinstance(outputs[elem], str) and (outputs[elem].startswith('http://') or outputs[elem].startswith('https://')):
              outputs[elem] = Markup("<a href='%s' target='_blank'>%s</a>" % (outputs[elem], outputs[elem]))

    return render_template('outputs.html', infid=infid, outputs=outputs)

@app.route('/delete/<infid>')
@authorized_with_valid_token
def infdel(infid=None):

    access_token = oidc_blueprint.session.token['access_token']
    auth_data = utils.getUserAuthData(access_token)
    headers = {"Authorization": auth_data}

    url = "%s/infrastructures/%s?async=1" % (settings.imUrl, infid)
    response = requests.delete(url, headers=headers)

    if not response.ok:
        flash("Error deleting infrastructure: " + response.text, "error");

    return redirect(url_for('showinfrastructures'))

@app.route('/configure')
@authorized_with_valid_token
def configure():

    access_token = oidc_blueprint.session.token['access_token']

    selected_tosca = request.args['selected_tosca']

    app.logger.debug("Template: " + json.dumps(toscaInfo[selected_tosca]))

    vos = appdb.get_vo_list()
    if session["vos"]:
        vos = [vo for vo in vos if vo in session["vos"]]

    return render_template('createdep.html',
                           template=toscaInfo[selected_tosca],
                           selectedTemplate=selected_tosca,
                           vos=vos)

@app.route('/sites/<vo>')
def getsites(vo=None):
    res = ""
    for site_name, (site_url, site_state) in appdb.get_sites(vo).items():
        res += '<option name="selectedSite" value=%s>%s</option>' % (site_name, site_name)
    return res

@app.route('/images/<site>/<vo>')
def getimages(site=None, vo=None):
    res = ""
    for image in appdb.get_images(site, vo):
        res += '<option name="selectedImage" value=%s>%s</option>' % (image, image)
    return res

def add_image_to_template(template, image):
    # Add the image to all compute nodes

    for node in list(template['topology_template']['node_templates'].values()):
        if node["type"] == "tosca.nodes.indigo.Compute":
            node["capabilities"]["os"]["properties"]["image"] = image

    app.logger.debug(yaml.dump(template, default_flow_style=False))

    return template

def add_auth_to_template(template, auth_data):
    # Add the auth_data ElasticCluster node

    for node in list(template['topology_template']['node_templates'].values()):
        if node["type"] == "tosca.nodes.ec3.ElasticCluster":
            if "properties" not in node:
                node["properties"] = {}
            node["properties"]["im_auth"] = auth_data

    app.logger.debug(yaml.dump(template, default_flow_style=False))

    return template

def set_inputs_to_template(template, inputs):
    # Add the image to all compute nodes

    for name, value in template['topology_template']['inputs'].items():
        if name in inputs:
            if value["type"] == "integer":
                value["default"] = int(inputs[name])
            elif value["type"] == "float":
                value["default"] = float(inputs[name])
            elif value["type"] == "boolean":
                if inputs[name].lower() in ['yes', 'true', '1']:
                    value["default"] = True
                else:
                    value["default"] = False
            else:
                value["default"] = inputs[name]

    app.logger.debug(yaml.dump(template, default_flow_style=False))

    return template

#        
# 
@app.route('/submit', methods=['POST'])
@authorized_with_valid_token
def createdep():

  access_token = oidc_blueprint.session.token['access_token']
  auth_data = utils.getUserAuthData(access_token)

  app.logger.debug("Form data: " + json.dumps(request.form.to_dict()))

  with io.open( settings.toscaDir + request.args.get('template')) as stream:
      template = yaml.full_load(stream)

      form_data = request.form.to_dict()

#      if form_data['extra_opts.schedtype'] == "man":
      image = "appdb://%s/%s?%s" % (form_data['extra_opts.selectedSite'],
                                    form_data['extra_opts.selectedImage'],
                                    form_data['extra_opts.selectedVO'])
      template = add_image_to_template(template, image)

      template = add_auth_to_template(template, auth_data)

      inputs = { k:v for (k,v) in form_data.items() if not k.startswith("extra_opts.") }

      app.logger.debug("Parameters: " + json.dumps(inputs))

      template = set_inputs_to_template(template, inputs)

      payload = yaml.dump(template,default_flow_style=False, sort_keys=False)

  headers = {"Authorization": auth_data, "Content-Type": "text/yaml"}

  url = "%s/infrastructures?async=1" % settings.imUrl
  response = requests.post(url, headers=headers, data=payload)

  if not response.ok:
     flash("Error creating infrastrucrure: \n" + response.text, "error")

  return redirect(url_for('showinfrastructures'))

@app.route('/manage_creds')
@authorized_with_valid_token
def manage_creds():
  sites={}

  try:
    sites = appdb.get_sites()
  except Exception as e:
    flash("Error retrieving sites list: \n" + str(e), 'warning')

  return render_template('service_creds.html', sites=sites)


@app.route('/write_creds', methods=['GET', 'POST'])
@authorized_with_valid_token
def write_creds():
    serviceid = request.args.get('service_id',"")
    app.logger.debug("service_id={}".format(serviceid))

    if request.method == 'GET':
      res = {}
      try:
          res = cred.get_cred(serviceid)
      except Exception as ex:
          flash("Error reading credentials %s!" % ex, 'error')

      return render_template('modal_creds.html', service_creds=res, service_id=serviceid)
    else:    
       app.logger.debug("Form data: " + json.dumps(request.form.to_dict()))

       creds = request.form.to_dict()
       try:
           cred.write_creds(serviceid, creds)
           flash("Credentials successfully written!", 'info')
       except Exception as ex:
           flash("Error writing credentials %s!" % ex, 'error')

       return redirect(url_for('manage_creds'))


@app.route('/delete_creds')
@authorized_with_valid_token
def delete_creds():

    serviceid = request.args.get('service_id',"")
    try:
        cred.delete_cred(serviceid)
        flash("Credentials successfully deleted!", 'info')
    except Exception as ex:
        flash("Error deleting credentials %s!" % ex, 'error')
    
    return redirect(url_for('manage_creds'))


@app.route('/addresourcesform/<infid>')
@authorized_with_valid_token
def addresourcesform(infid=None):

    access_token = oidc_blueprint.session.token['access_token']

    auth_data = utils.getUserAuthData(access_token)
    headers = {"Authorization": auth_data, "Accept": "text/plain"}

    url = "%s/infrastructures/%s/radl" % (settings.imUrl, infid)
    response = requests.get(url, headers=headers)

    if response.ok:
        try:
            radl = radl_parse.parse_radl(response.text)
        except Exception as ex:
            flash("Error parsing RADL: \n%s" % str(ex), 'error')

        return render_template('addresource.html', infid=infid, systems=radl.systems)
    else:
        flash("Error getting RADL: \n%s" % (response.text), 'error')
        return redirect(url_for('showinfrastructures'))


@app.route('/addresources/<infid>')
@authorized_with_valid_token
def addresources(infid=None):

    access_token = oidc_blueprint.session.token['access_token']

    auth_data = utils.getUserAuthData(access_token)
    headers = {"Authorization": auth_data, "Accept": "text/plain"}

    form_data = request.form.to_dict()

    url = "%s/infrastructures/%s/radl" % (settings.imUrl, infid)
    response = requests.get(url, headers=headers)

    if response.ok:
        try:
            radl = radl_parse.parse_radl(response.text)
            radl.deploys = []
            for system in radl.systems:
                vm_num = int(form_data["%s_num" % system.name])
                if vm_num > 0:
                    radl.deploys.append(deploy(system.name, vm_num))
        except Exception as ex:
            flash("Error parsing RADL: \n%s\n%s" % (str(ex), response.text), 'error')

        url = "%s/infrastructures/%s" % (settings.imUrl, infid)
        response = requests.post(url, headers=headers, data=str(radl))

        if response.ok:
            flash("Nodes added successfully", 'info')
        else:
            flash("Error adding nodesL: \n%s" % (response.text), 'error')
        
        return redirect(url_for('showinfrastructures'))
    else:
        flash("Error getting RADL: \n%s" % (response.text), 'error')
        return redirect(url_for('showinfrastructures'))


@app.route('/logout')
def logout():
   session.clear()
   oidc_blueprint.session.get("/logout")
   return redirect(url_for('login'))
