########################################################
consumer_name = 'api_oneapi_consumer'
import os, sys
import time
from pathlib import Path
sys.path.insert(0, os.path.dirname(Path(__file__).parents[1]))

import logging
logger = logging.getLogger(__name__)

from datetime import datetime
import base64
import json
import threading
import requests
from requests.packages.urllib3.exceptions import InsecureRequestWarning
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

########################################################

# set timeout duration (seconds)
timeout_duration = 25

# -------------------------------------------------------
# OneAPI Base URLs
# Icinga had: dev  → https://io-api-lab.aexp.com/silence
#             qa   → https://qa-tims-icinga.aexp.com
#             prod → https://phx-tims-icinga.aexp.com
# -------------------------------------------------------
ONEAPI_BASE_URLS = {
    'dev':  'https://io-api-lab.aexp.com',
    'qa':   'https://io-api-lab.aexp.com',   # TODO: update when QA URL confirmed
    'prod': 'https://io-api-lab.aexp.com',   # TODO: update when PROD URL confirmed
}

# AuthBlue token endpoint  (replaces Icinga /icingaweb2/authentication/login)
AUTHBLUE_TOKEN_URL = 'https://authbluetokens.aexp.com/v1/app2app/tokens'

# OneAPI service account
ONEAPI_SERVICE_ID = os.environ.get('ONEAPI_SERVICE_ID', 'svc.oneapi-e2')


class ConsumerException(Exception):
    def __init__(self, message):
        self.message = message
        super().__init__(message)


# =====================================================================
# Helper – datetime conversion
# Icinga used Unix int timestamps; OneAPI silence uses ISO strings
# =====================================================================
def _to_iso(value):
    """Convert dd/MM/YYYY HH:MM  or Unix timestamp  →  YYYY-MM-DDTHH:MM:SS"""
    if not value:
        return ''
    if isinstance(value, int) or (isinstance(value, str) and str(value).isdigit()):
        return datetime.utcfromtimestamp(int(value)).strftime('%Y-%m-%dT%H:%M:%S')
    for fmt in ('%d/%m/%Y %H:%M', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(str(value), fmt).strftime('%Y-%m-%dT%H:%M:%S')
        except ValueError:
            continue
    return str(value)


# =====================================================================
# Base Action Class
# Replaces Icinga Action which used NTLM/Basic session auth +
# get_session() / get_login() / icinga_request()
# =====================================================================
class OneAPIAction:
    """Base class for all OneAPI actions.

    Key differences from Icinga Action:
    - No get_session() / get_login()  – uses AuthBlue Bearer token instead
    - No api_session with NTLM auth
    - base_url → https://io-api-lab.aexp.com  (not Icinga Director URL)
    """

    def __init__(self, data: dict):
        self.data = data
        self.action = data.get('action', '')
        self.headers = {
            'Content-Type': 'application/json',
            'Accept':        'application/json',
        }
        self.errors        = []
        self.auth_token    = None
        self.base_url      = ''
        self.method        = 'POST'
        self.path          = ''
        self.query_string  = ''
        self.body          = {}
        self.required_keys = []
        self.object_type   = 'host'

        self._resolve_base_url()
        self._acquire_auth_token()

    # ------------------------------------------------------------------
    # URL resolution  (mirrors get_scipt_env logic in icinga wrapper)
    # ------------------------------------------------------------------
    def _resolve_base_url(self):
        if self.data.get('api_url'):
            self.base_url = self.data['api_url']
        else:
            env = os.environ.get('ONEAPI_ENV', 'dev').lower()
            self.base_url = ONEAPI_BASE_URLS.get(env, ONEAPI_BASE_URLS['dev'])
        logger.info('OneAPI base_url: {}'.format(self.base_url))

    # ------------------------------------------------------------------
    # Authentication
    # Replaces: get_session() → session.auth = NTLM
    #           get_login()   → GET /icingaweb2/authentication/login
    #
    # Priority:
    #   1. data['token']          – passed via --token CLI arg
    #   2. ONEAPI_BEARER_TOKEN    – env var with pre-fetched token
    #   3. ONEAPI_BASIC_AUTH      – pre-encoded Basic auth string (skips password build)
    #   4. ONEAPI_SERVICE_PASSWORD – builds Basic auth from service_id + password
    # ------------------------------------------------------------------
    def _acquire_auth_token(self):
        # 1 & 2 — pre-fetched Bearer token → skip AuthBlue
        direct_token = self.data.get('token', '') or os.environ.get('ONEAPI_BEARER_TOKEN', '')
        if direct_token:
            self.auth_token = direct_token
            self.headers['Authorization'] = f'Bearer {self.auth_token}'
            logger.info('Using pre-set Bearer token.')
            return

        # 3 — pre-encoded Basic auth string
        basic_auth = os.environ.get('ONEAPI_BASIC_AUTH', '')
        if not basic_auth:
            # 4 — build from service_id + password
            service_password = os.environ.get('ONEAPI_SERVICE_PASSWORD', '')
            if not service_password:
                logger.warning(
                    'No token source found. Set --token, ONEAPI_BEARER_TOKEN, '
                    'ONEAPI_BASIC_AUTH, or ONEAPI_SERVICE_PASSWORD.'
                )
            raw = f'{ONEAPI_SERVICE_ID}:{service_password}'
            basic_auth = base64.b64encode(raw.encode()).decode()

        # Correct AuthBlue request body (from reference curl)
        auth_body = {
            "scope": {
                "attributes": ["givenname", "sn", "department"],
                "groups": ["infraobserve-test"]
            }
        }
        auth_headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Basic {basic_auth}',
        }

        try:
            response = requests.post(
                AUTHBLUE_TOKEN_URL,
                headers=auth_headers,
                json=auth_body,
                verify=False,
                timeout=timeout_duration
            )
            if response.ok:
                token_data = response.json()
                self.auth_token = (
                    token_data.get('access_token')
                    or token_data.get('token')
                    or token_data.get('id_token')
                )
                self.headers['Authorization'] = f'Bearer {self.auth_token}'
                logger.info('Successfully obtained AuthBlue Bearer token.')
            else:
                msg = f'AuthBlue token request failed: {response.status_code} {response.text}'
                logger.error(msg)
                raise ConsumerException(msg)
        except requests.ConnectionError as e:
            msg = f'Unable to connect to AuthBlue: {e}'
            logger.error(msg)
            raise ConsumerException(msg)

    # ------------------------------------------------------------------
    @property
    def endpoint_url(self):
        return str(self.base_url).rstrip('/') + self.path + self.query_string

    @property
    def required(self):
        return {k: self.data[k] for k in self.required_keys if k in self.data}

    # ------------------------------------------------------------------
    # HTTP request  (replaces icinga_request)
    # ------------------------------------------------------------------
    def oneapi_request(self):
        _body = json.dumps(self.body)
        logger.info('{s} HTTP VERB:  {v}'.format(s=threading.current_thread(), v=self.method))
        logger.info('{s} ENDPOINT:   {u}'.format(s=threading.current_thread(), u=self.endpoint_url))
        logger.info('{s} HEADERS:    {h}'.format(s=threading.current_thread(), h=self.headers))
        logger.info('{s} BODY:       {b}'.format(s=threading.current_thread(), b=_body))

        try:
            if self.method == 'GET':
                response = requests.get(
                    self.endpoint_url, headers=self.headers,
                    verify=False, timeout=timeout_duration
                )
            elif self.method == 'DELETE':
                response = requests.delete(
                    self.endpoint_url, headers=self.headers,
                    json=self.body, verify=False, timeout=timeout_duration
                )
            elif self.method == 'PUT':
                response = requests.put(
                    self.endpoint_url, headers=self.headers,
                    json=self.body, verify=False, timeout=timeout_duration
                )
            else:  # POST
                response = requests.post(
                    self.endpoint_url, headers=self.headers,
                    json=self.body, verify=False, timeout=timeout_duration
                )

            response_json = {"message": "", "reason": "", "status": ""}
            try:
                response_json = response.json()
                logger.info("JSON Response: " + str(response_json))
            except (json.JSONDecodeError, ValueError):
                response_json["message"] = response.text
                response_json["reason"]  = response.reason
                response_json["status"]  = "completed" if response.ok else "failed"

            if response.ok:
                logger.info('API Call was successful')
            else:
                logger.error('Something went wrong')
                logger.error("Status: {} | Reason: {}".format(
                    response.status_code, response.reason
                ))
            return response_json

        except Exception as e:
            msg = str(getattr(e, 'message', e))
            logger.error('Unexpected error! ' + msg)
            raise ConsumerException(msg)

    def output(self):
        self.data['jl_response'] = self.oneapi_request()
        self.data['jd_response'] = json.dumps(self.data['jl_response'], indent=4)
        return self.data


# =====================================================================
# Query Actions  (read-only)
# Base class = QueryAction in Icinga (self.http_verb = requests.get)
# =====================================================================

class GetHost(OneAPIAction):
    """Get a single host record.

    Icinga:  GET /icingaweb2/director/host?name={host_name}
    OneAPI:  GET /hosts/{host_name}
    """
    def __init__(self, data: dict):
        super().__init__(data)
        self.object_type   = 'host'
        self.action        = 'getHost'
        self.method        = 'GET'
        self.path          = '/hosts/{}'.format(data.get('host_name', ''))
        self.query_string  = ''
        self.required_keys = ['host_name']
        self.body          = {}


class GetAllHost(OneAPIAction):
    """Get all hosts matching a pattern.

    Icinga:  GET /icingaweb2/director/hosts?q={host_name}
    OneAPI:  GET /hosts?q={host_name}
    """
    def __init__(self, data: dict):
        super().__init__(data)
        self.object_type   = 'host'
        self.action        = 'getAllHost'
        self.method        = 'GET'
        self.path          = '/hosts'
        self.query_string  = '?q={}'.format(data.get('host_name', ''))
        self.required_keys = ['host_name']
        self.body          = {}


class GetHostServices(OneAPIAction):
    """Get all services for a host.

    Icinga:  GET /icingaweb2/monitoring/list/services?host={host_name}
    OneAPI:  GET /hosts/{host_name}/services
    """
    def __init__(self, data: dict):
        super().__init__(data)
        self.object_type   = 'host'
        self.action        = 'getHostServices'
        self.method        = 'GET'
        self.path          = '/hosts/{}/services'.format(data.get('host_name', ''))
        self.query_string  = ''
        self.required_keys = ['host_name']
        self.body          = {}


# =====================================================================
# Manage Actions  (create / update / decommission)
# Base class = ManageAction in Icinga (self.http_verb = requests.post)
# =====================================================================

class AddESXi(OneAPIAction):
    """[CAUT-1576] Create new endpoint for onboarding new objects (ESXi) to OneAPI.

    Icinga:  POST /icingaweb2/director/host
             body: object_name, object_type, address, zone, vars.*, imports
    OneAPI:  POST /hosts   (same body schema)
    """
    def __init__(self, data: dict):
        super().__init__(data)
        self.object_type   = 'host'
        self.action        = 'addHost'
        self.method        = 'POST'
        self.path          = '/hosts'
        self.query_string  = ''
        self.required_keys = ['location', 'hardwaretype', 'project', 'environment']

        # PHX Instance zone mapping  (same as Icinga)
        self.env_zone_mapping = {
            "dev":  "E1Checker",
            "qa":   "E2Checker",
            "gdha": "GDHAChecker",
        }

        # Cisco hardware is always standalone
        if data.get('hardwaretype') == 'cisco':
            data['hardwaresubtype'] = 'standalone'

        self.body = {
            "object_name":          data.get('host_name', ''),
            "object_type":          "object",
            "address":              data.get('host_name', ''),
            "zone":                 self.env_zone_mapping.get(data.get('environment', ''), ''),
            "vars.env":             data.get('environment', ''),
            "vars.project":         data.get('project', ''),
            "vars.location":        data.get('location', ''),
            "vars.hardwaretype":    data.get('hardwaretype', ''),
            "#vars.hardwaresubtype": data.get('hardwaresubtype', ''),
            "vars.shortname":       data.get('host_name', '').split('.')[0],
            "vars.sn_assignment_queue": data.get('sn_assignment_queue', 'Infrastructure'),
            "vars.sn_reporter_group":   data.get('sn_reporter_group', 'Infrastructure'),
            "imports":              "003-esx-automation-host-defaults",
            "#vars.hydra":          "host",
        }

        if data.get('hydra') == 'true':
            self.body['vars.hydra'] = self.action
        if data.get('hardwaresubtype'):
            self.body['vars.hardwaresubtype'] = data['hardwaresubtype']
        if data.get('zone'):
            self.body['zone'] = data['zone']


class AddCluster(OneAPIAction):
    """[CAUT-1576] Create new endpoint for onboarding new objects (Clusters) to OneAPI.

    Icinga:  POST /icingaweb2/director/host
             body: object_name, object_type, address, zone, vars.clustername, imports
    OneAPI:  POST /hosts   (same body schema)
    """
    def __init__(self, data: dict):
        super().__init__(data)
        self.object_type   = 'cluster'
        self.action        = 'addCluster'
        self.method        = 'POST'
        self.path          = '/hosts'
        self.query_string  = ''
        self.required_keys = ['environment', 'host_name']

        # PHX Instance zone mapping  (same as Icinga)
        self.env_zone_mapping = {
            "dev":  "E1Checker",
            "qa":   "E2Checker",
            "gdha": "GDHAChecker",
        }

        split_string = data.get('host_name', '').split('-')

        self.body = {
            "object_name":  data.get('host_name', ''),
            "object_type":  "object",
            "address":      data.get('host_name', ''),
            "zone":         self.env_zone_mapping.get(data.get('environment', ''), ''),
            "vars.env":     data.get('environment', ''),
            "imports":      "003-esx-automation-cluster-defaults",
            "vars.sn_assignment_queue": data.get('sn_assignment_queue', 'Infrastructure'),
            "vars.sn_reporter_group":   data.get('sn_reporter_group', 'Infrastructure'),
            "#vars.hydra":  "host",
            "vars.clustername": "-".join(split_string[1:]),
        }

        if data.get('hydra') == 'true':
            self.body['vars.hydra'] = self.action
        if data.get('zone'):
            self.body['zone'] = data['zone']


class DisableObject(OneAPIAction):
    """[CAUT-1577] Create new endpoint for Disabling objects on OneAPI.

    Icinga:  POST /icingaweb2/director/host?name={host_name}
             body: { object_name: host_name, disabled: 'y' }
    OneAPI:  POST /hosts/{host_name}
             body: { object_name: host_name, disabled: true }

    Note: Icinga used string 'y'/'n' → OneAPI uses boolean true/false.
    """
    def __init__(self, data: dict):
        super().__init__(data)
        self.object_type   = 'host'
        self.action        = 'disableHost'
        self.method        = 'POST'
        self.path          = '/hosts/{}'.format(data.get('host_name', ''))
        self.query_string  = ''
        self.required_keys = ['host_name']
        self.body = {
            "object_name": data.get('host_name', ''),
            "disabled":    True,           # Icinga used 'y', OneAPI uses boolean
        }


class EnableObject(OneAPIAction):
    """[CAUT-1580] Create new endpoint for Enabling objects on OneAPI.

    Icinga:  POST /icingaweb2/director/host?name={host_name}
             body: { object_name: host_name, disabled: 'n' }
    OneAPI:  POST /hosts/{host_name}
             body: { object_name: host_name, disabled: false }
    """
    def __init__(self, data: dict):
        super().__init__(data)
        self.object_type   = 'host'
        self.action        = 'enableHost'
        self.method        = 'POST'
        self.path          = '/hosts/{}'.format(data.get('host_name', ''))
        self.query_string  = ''
        self.required_keys = ['host_name']
        self.body = {
            "object_name": data.get('host_name', ''),
            "disabled":    False,          # Icinga used 'n', OneAPI uses boolean
        }


# =====================================================================
# Silence / Blackout Actions
# =====================================================================

class ScheduleSilence(OneAPIAction):
    """[CAUT-1578] Create new endpoint for Scheduling Downtime (silence) on OneAPI.

    Icinga:  POST /icingaweb2/monitoring/host/schedule-downtime?host={host_name}
             body: { type: fixed, start: unix_ts, end: unix_ts,
                     author: requestor, comment: comment, all_services: True }
    OneAPI:  POST /silence
             body: { comment, end_time_mst: ISO, host_names: [...], start_time_mst: ISO }

    Changes:
      - Path: schedule-downtime  →  /silence
      - start/end: Unix timestamp  →  ISO datetime string
      - author/all_services fields removed (not in OneAPI schema)
    """
    def __init__(self, data: dict):
        super().__init__(data)
        self.object_type   = 'host'
        self.action        = 'scheduleSilence'
        self.method        = 'POST'
        self.path          = '/silence'
        self.query_string  = ''
        self.required_keys = ['host_name', 'start', 'end', 'comment', 'requestor']

        self.body = {
            "comment":        data.get('comment', ''),
            "end_time_mst":   _to_iso(data.get('end', '')),
            "host_names":     [data['host_name']] if data.get('host_name') else [],
            "start_time_mst": _to_iso(data.get('start', '')),
        }


class GetSilenceStatus(OneAPIAction):
    """Check the status of a submitted silence job.

    Icinga:  No equivalent
    OneAPI:  GET /silence/{job_id}
             Returns: { job_id, status, host_names }
    """
    def __init__(self, data: dict):
        super().__init__(data)
        self.action        = 'getSilenceStatus'
        self.method        = 'GET'
        self.path          = '/silence/{}'.format(data.get('job_id', ''))
        self.query_string  = ''
        self.required_keys = ['job_id']
        self.body          = {}


class RemoveSilence(OneAPIAction):
    """[CAUT-1579] Create new endpoint for removing Downtime from object on OneAPI.

    Icinga:  POST /icingaweb2/monitoring/downtimes/delete-all?host={host_name}
             body: {}
    OneAPI:  DELETE /silence/{job_id}   (if job_id known)
             POST   /silence/remove     (fallback by host_name)
    """
    def __init__(self, data: dict):
        super().__init__(data)
        self.object_type   = 'host'
        self.action        = 'removeSilence'
        self.required_keys = ['host_name', 'requestor']

        job_id = data.get('job_id', '')
        if job_id:
            self.method       = 'DELETE'
            self.path         = '/silence/{}'.format(job_id)
            self.query_string = ''
            self.body         = {}
        else:
            # Fallback: remove by host_name
            self.method       = 'POST'
            self.path         = '/silence/remove'
            self.query_string = ''
            self.body         = {
                "host_names": [data.get('host_name', '')],
                "comment":    data.get('comment', ''),
            }
