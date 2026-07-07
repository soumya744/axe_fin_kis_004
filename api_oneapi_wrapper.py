# == [ API CALL FUNCTIONS ] ==========================================
from api_oneapi_models import (
    AddCluster, AddESXi, GetHost, GetAllHost,
    DisableObject, EnableObject,
    ScheduleSilence, RemoveSilence, GetSilenceStatus,
    ConsumerException,
)
import os, sys, socket
from pathlib import Path
sys.path.insert(0, os.path.dirname(Path(__file__).parents[1]))

import logging
logger = logging.getLogger(__name__)

import time, json
from datetime import datetime

"""
Functions that are called from the api/views.py
"""

DEV_ENV = True
DB_TABLE = 'dids_api_req'
PG_STATUS_FAIL     = {'req_status_cd': 'Failed'}
PG_STATUS_COMPLETE = {'req_status_cd': 'Completed'}

# Environment URL mapping (kept for backwards compatibility with get_scipt_env)
env_url_mapping = {
    "LAB": "https://io-api-lab.aexp.com",
    "QA":  "https://io-api-lab.aexp.com",   # TODO: update when QA URL confirmed
    "GSO": "https://io-api-lab.aexp.com",   # TODO: update when PROD URL confirmed
    "PHX": "https://io-api-lab.aexp.com",   # TODO: update when PROD URL confirmed
}

hostname = socket.gethostname()
if hostname.find("hvnpl") >= 0:
    DEV_ENV = False


# ------------------------------------------------------------------
# Environment / URL helpers
# ------------------------------------------------------------------

def get_scipt_env(data):
    """Resolve the OneAPI base URL for the current environment.

    Mirrors the original get_scipt_env() logic but targets OneAPI URLs
    instead of Icinga Director URLs.
    """
    logger.info("Getting API URL from environment..")
    data["api_url"] = env_url_mapping.get("LAB", "https://io-api-lab.aexp.com")

    if not DEV_ENV:
        if "host_name" in data and data["host_name"]:
            # Keep GSO/PHX split logic for future prod URLs
            if "gso.aexp.com" in data["host_name"]:
                data["api_url"] = env_url_mapping["GSO"]
            elif "phx.aexp.com" in data["host_name"]:
                data["api_url"] = env_url_mapping["PHX"]

        if "location" in data and data["location"] == "ipc2":
            data["api_url"] = env_url_mapping["GSO"]

    logger.info("API URL: " + data["api_url"])
    return data


# ------------------------------------------------------------------
# User validation  (unchanged – still calls LDAP)
# ------------------------------------------------------------------

def validate_requestor(data):
    """Validate the requestor against Active Directory via LDAP consumer."""
    try:
        import ldap_consumer
        LDAPINFO = {}
        LDAPINFO['ldap_action'] = 'email'
        LDAPINFO['email'] = ''
        LDAPINFO['user'] = data['requestor']
        data["subject"] = "OneAPI - {}".format(data['action'])
        data['valid_user'] = False
        try:
            data['email'] = ldap_consumer.main(LDAPINFO)
            data['valid_user'] = True
        except Exception:
            msg = "User {0} was not found in AD. Please use a valid user ID &#10060;.".format(
                data.get('requestor', '')
            )
            data = build_custom_pg_error(data, msg)
    except ImportError:
        # ldap_consumer not available in local test mode
        logger.warning("ldap_consumer not available – skipping AD validation.")
        data['valid_user'] = True
        data['email'] = data.get('requestor', '')
    return data


# ------------------------------------------------------------------
# Email notification  (unchanged logic)
# ------------------------------------------------------------------

def send_email(data):
    try:
        import smtp_consumer
        logger.info("====> Sending Email....")
        data["sender"] = "dids_automation_communications@aexp.com"
        # Strip internal tracking keys before sending
        if 'pg_data' in data:
            keys_to_remove = ["pg_data", "pg_updates", "db_table", "fields",
                               "values", "output_body_da"]
            for key in keys_to_remove:
                if key in data:
                    del data[key]

        if "message" not in data:
            data["message"] = ""
        items = data.items()
        avoid_keys = ["message", "subject", "sender", "jl_response", "jd_response"]
        for key, value in items:
            if key not in avoid_keys:
                data["message"] += "<strong><i>{0}:</i></strong> {1}<br>".format(
                    key.replace('_', ' ').title(), value
                )

        if DEV_ENV:
            data["subject"] = data["subject"] + " LAB "
            data["cc_emails"] = ["eric.cano@aexp.com"]
        else:
            data["subject"] = data["subject"] + " PROD "
            data["cc_emails"] = ["eric.cano@aexp.com"]

        smtp_consumer.main(data)
    except ImportError:
        logger.warning("smtp_consumer not available – skipping email notification.")
    return


# ------------------------------------------------------------------
# Core action dispatcher
# ------------------------------------------------------------------

def process_action(action, data):
    """Instantiate the action class, call it, persist to PG, return output."""
    data["subject"] = "OneAPI - {}".format(data['action'])

    # PG tracking data
    data['pg_updates'] = {}
    data['pg_data'] = {
        'user_id':         data.get('requestor', ''),
        'endpoint_da':     '',
        'input_body_da':   json.dumps(data),
        'req_url_tx':      '',
        'req_status_cd':   'In Process',
        'consumer_cd':     'oneapi_consumer',
        'api_call_repeat_in': False,
        'req_ts':          'NOW()',
        'last_update_ts':  'NOW()',
    }

    try:
        import postgres_consumer as pg_consumer
        data = build_pg_create_info(data)
        data.update(*pg_consumer.create_row(data))
    except Exception:
        logger.warning("postgres_consumer not available – skipping DB row creation.")

    try:
        action_cls = action(data)
        output = action_cls.output()
        data['pg_updates'].update(PG_STATUS_COMPLETE)
    except ConsumerException as e:
        data['icinga_consumer_error'] = e.message
        data['pg_updates'].update(PG_STATUS_FAIL)
        output = data

    if 'jd_response' in data:
        data['pg_updates']['output_body_da'] = data['jd_response']

    try:
        import postgres_consumer as pg_consumer
        data['pg_updates_query_string'] = build_pg_update_info(data)
        pg_consumer.update_row(data)
    except Exception:
        logger.warning("postgres_consumer not available – skipping DB row update.")

    # Send email for ManageAction subclasses or on error
    from api_oneapi_models import OneAPIAction
    if isinstance(action_cls if 'action_cls' in dir() else None, OneAPIAction):
        if 'send_email' in data and data.get('send_email', '').lower() == 'true':
            send_email(data)

    return output


# ------------------------------------------------------------------
# PG helpers  (unchanged logic)
# ------------------------------------------------------------------

def build_pg_create_info(data):
    data['db_table'] = DB_TABLE
    data['fields']   = ', '.join(list(data.get('pg_data', {}).keys()))
    data['values']   = ', '.join(
        f'%({key})s' for key in list(data.get('pg_data', {}).keys())
    )
    return data


def build_pg_update_info(data):
    return ', '.join(
        f'{key} = %({key})s' for key in list(data.get('pg_updates', {}).keys())
    )


def build_custom_pg_error(data, error_message):
    data['pg_updates'] = {}
    data = build_pg_create_info(data)
    try:
        import postgres_consumer as pg_consumer
        data.update(*pg_consumer.create_row(data))
    except Exception:
        pass
    logger.error(error_message)
    data['jd_response'] = {"error": error_message}
    data['pg_updates'].update(PG_STATUS_FAIL)
    data['pg_updates']['output_body_da'] = json.dumps(data['jd_response'])
    try:
        import postgres_consumer as pg_consumer
        data['pg_updates_query_string'] = build_pg_update_info(data)
        pg_consumer.update_row(data)
    except Exception:
        pass
    send_email(data)
    return data


# ------------------------------------------------------------------
# Date conversion  (Icinga used dd/mm/YYYY HH:MM → OneAPI wants ISO)
# ------------------------------------------------------------------

def convert_dates(data):
    """Convert input datetime strings to ISO format for OneAPI.

    Icinga used Unix timestamps internally; OneAPI silence endpoint
    expects ISO strings like '2026-06-19T03:20:00'.
    """
    from api_oneapi_models import _to_iso

    datetime_str_start = data.get('start', '')
    datetime_str_end   = data.get('end',   '')

    print("Start Time:", datetime_str_start)
    print("End Time:",   datetime_str_end)

    data["start_time"] = datetime_str_start
    data["end_time"]   = datetime_str_end

    # Convert to ISO and store back (ScheduleSilence reads 'start' / 'end')
    data['start'] = _to_iso(datetime_str_start)
    data['end']   = _to_iso(datetime_str_end)

    return data


# ------------------------------------------------------------------
# Public API functions  (called from views.py / consumer)
# ------------------------------------------------------------------

def create_esxi(data):
    """Onboard a new ESXi host. (CAUT-1576)"""
    if data.get("hardwaretype") == "cisco":
        data["hardwaresubtype"] = "standalone"
    validate_requestor(data)
    if data['valid_user']:
        output = process_action(AddESXi, data)
        return output
    else:
        return data['jd_response']


def create_cluster(data):
    """Onboard a new Cluster. (CAUT-1576)"""
    validate_requestor(data)
    if data['valid_user']:
        output = process_action(AddCluster, data)
        return output
    else:
        return data['jd_response']


def get_host(data):
    """Fetch a single host record."""
    output = process_action(GetHost, data)
    return output


def get_host_services(data):
    """Not directly supported in OneAPI – placeholder."""
    logger.warning("get_host_services: no equivalent OneAPI endpoint yet.")
    return {"message": "Not implemented for OneAPI yet.", "status": "skipped"}


def disable_esxi(data):
    """Disable an ESXi host in OneAPI/Grafana. (CAUT-1577)"""
    validate_requestor(data)
    if data['valid_user']:
        output = process_action(DisableObject, data)
        return output
    else:
        return data['jd_response']


def enable_esxi(data):
    """Enable an ESXi host in OneAPI/Grafana. (CAUT-1577)"""
    validate_requestor(data)
    if data['valid_user']:
        output = process_action(EnableObject, data)
        return output
    else:
        return data['jd_response']


def schedule_esxi_blackout(data):
    """Schedule a silence/blackout via OneAPI. (CAUT-1578)"""
    validate_requestor(data)
    if data['valid_user']:
        convert_dates(data)

        import time as _time
        current_timestamp = int(_time.time())
        if data['start'] < current_timestamp or data['end'] < current_timestamp:
            msg = "Start and end times must be in the future."
            data = build_custom_pg_error(data, msg)
            return data
        if data['start'] >= data['end']:
            msg = "Start time must be before end time."
            data = build_custom_pg_error(data, msg)
            return data

        output = process_action(ScheduleSilence, data)
        return output
    else:
        return data['jd_response']


def remove_esxi_blackout(data):
    """Remove an active silence/blackout via OneAPI. (CAUT-1579)"""
    validate_requestor(data)
    if data['valid_user']:
        output = process_action(RemoveSilence, data)
        return output
    else:
        return data['jd_response']


def get_silence_status(data):
    """Check the status of a submitted silence job."""
    output = process_action(GetSilenceStatus, data)
    return output
