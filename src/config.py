import os
import json
import yaml
from datetime import timedelta
import misc
import kvtable
import pdb
import debug as Dbg

import cslog
log = cslog.logger(__name__)

ctx   = None
_init = None

from aws_xray_sdk.core import xray_recorder

@xray_recorder.capture(name="config.init")
def init(context, with_kvtable=True, with_predefined_configuration=True):
    global _init
    _init = {}
    _init["context"] = context
    _init["all_configs"] = [{
        "source": "Built-in defaults",
        "config": {},
        "metas" : {}
        }]
    if with_kvtable:
        _init["configuration_table"] = kvtable.KVTable(context, context["ConfigurationTable"])
        _init["configuration_table"].reread_table()
    _init["with_kvtable"] = with_kvtable
    _init["active_parameter_set"] = None
    register({
             "config.dump_configuration,Stable" : {
                 "DefaultValue": "0",
                 "Format"      : "Bool",
                 "Description" : """Display all relevant configuration parameters in CloudWatch logs.

    Used for debugging purpose.
                 """
             },
             "config.loaded_files,Stable" : {
                 "DefaultValue" : "internal:predefined.config.yaml;internal:custom.config.yaml",
                 "Format"       : "StringList",
                 "Description"  : """A semi-column separated list of URL to load as configuration.

Upon startup, CloneSquad will load the listed files in sequence and stack them allowing override between layers.

The default contains a reference to the empty internal file 'custom.config.yaml'. Users that intend to embed
customization directly inside the Lambda delivery should override this file with their own configuration. See 
[Customizing the Lambda package](#customizing-the-lambda-package).

This key is evaluated again after each URL parsing meaning that a layer can redefine the 'config.loaded_files' to load further
YAML files.
                 """
             },
             "config.max_file_hierarchy_depth" : 10,
             "config.active_parameter_set,Stable": {
                 "DefaultValue": "",
                 "Format"      : "String",
                 "Description" : """Defines the parameter set to activate.

See [Parameter sets](#parameter-sets) documentation.
                 """
             },
             "config.default_ttl": 0
    })

    # Load extra configuration from specified URLs
    xray_recorder.begin_subsegment("config.init:load_files")
    files_to_load = get_list("config.loaded_files", default=[]) if with_predefined_configuration else []
    if "ConfigurationURL" in context:
        files_to_load.extend(context["ConfigurationURL"].split(";"))
    if misc.is_sam_local():
        # For debugging purpose. Ability to override config when in SAM local
        resource_file = "internal:sam.local.config.yaml" 
        log.info("Reading local resource file %s..." % resource_file)
        files_to_load.append(resource_file)


    loaded_files = []
    i = 0
    while i < len(files_to_load):
        f = files_to_load[i]
        if f == "": 
            i += 1
            continue

        fd = None
        c  = None
        try:
            fd = misc.get_url(f, throw_exception_on_warning=True)
            c  = yaml.safe_load(fd)
            if c is None: raise Exception("Failed to parse YAML!")
            loaded_files.append({
                    "source": f,
                    "config": c
                })
            if "config.loaded_files" in c:
                files_to_load.extend(c["config.loaded_files"].split(";"))
            if i > get_int("config.max_file_hierarchy_depth"):
                log.warning("Too much config file loads (%s)!! Stopping here!" % loaded_files) 
                break
        except Exception as e:
            if fd  is None: 
                log.warning("Failed to load config file '%s'! %s (Notice: It will be safely ignored!)" % (f, e))
            elif c is None: 
                log.warning("Failed to parse config file '%s'! %s (Notice: It will be safely ignored!)" % (f, e))
            else: 
                log.exception("Failed to process config file '%s'! (Notice: It will be safely ignored!)" % f)
        i += 1
    _init["loaded_files"] = loaded_files
    xray_recorder.end_subsegment()

    builtin_config = _init["all_configs"][0]["config"]
    for cfg in _get_config_layers(reverse=True):
        c = cfg["config"]
        if "config.active_parameter_set" in c:
            if c == builtin_config and isinstance(c, dict):
                _init["active_parameter_set"] = c["config.active_parameter_set"]["DefaultValue"]
            else:
                _init["active_parameter_set"] = c["config.active_parameter_set"]
            break
    _parameterset_sanity_check()

    register({
        "config.ignored_warning_keys,Stable" : {
            "DefaultValue": "",
            "Format"      : "StringList",
            "Description" : """A list of config keys that are generating a warning on usage, to disable them.

Typical usage is to avoid the 'WARNING' Cloudwatch Alarm to trigger when using a non-Stable configuration key.

    Ex: ec2.schedule.key1;ec2.schedule.key2

    Remember that using non-stable configuration keys, is creating risk as semantic and/or existence could change 
    from CloneSquad version to version!
            """
            }
        })
    _init["ignored_warning_keys"] = get_list_of_dict("config.ignored_warning_keys")


def _parameterset_sanity_check():
    # Warn user if parameter set is not found
    active_parameter_set = _init["active_parameter_set"]
    if active_parameter_set is not None and active_parameter_set != "":
        found = False
        for cfg in _get_config_layers(reverse=True):
            c = cfg["config"]
            if _init["active_parameter_set"] in c:
                found = True
                break
        if not found:
            log.warning("Active parameter set is '%s' but no parameter set with this name exists!" % _init["active_parameter_set"])

    

def register(config, ignore_double_definition=False):
    if _init is None:
        return
    builtin_config = _init["all_configs"][0]["config"]
    builtin_metas  = _init["all_configs"][0]["metas"]
    for c in config:
        p = misc.parse_line_as_list_of_dict(c)
        key = p[0]["_"]
        if not ignore_double_definition and key in builtin_config:
            raise Exception("Double definition of key '%s'!" % key)
        builtin_config[key] = config[c]
        builtin_metas[key]  = dict(p[0])

def _get_config_layers(reverse=False):
    l = []
    # Add built-in config
    l.extend(_init["all_configs"])
    # Add file loaded config
    if "loaded_files" in _init:
        l.extend(_init["loaded_files"])
    if _init["with_kvtable"]:
        # Add DynamoDB based configuration
        l.extend([{
            "source": "DynamoDB configuration table '%s'" % _init["context"]["ConfigurationTable"],
            "config": _init["configuration_table"].get_dict()}])
    l = l.copy()
    if reverse: l.reverse()
    return l

def is_stable_key(key):
    all_configs          = _init["all_configs"]
    metas                = all_configs[0]["metas"]
    return key in metas and "Stable" in metas[key] and metas[key]["Stable"]

def keys(prefix=None, only_stable_keys=False):
    all_configs          = _init["all_configs"]
    active_parameter_set = _init["active_parameter_set"]
    k                    = []
    config_layers        = _get_config_layers()
    for config_layer in _get_config_layers():
        c     = config_layer["config"]
        for key in c:
            if key.startswith("#"): continue # Ignore commented keys
            if key.startswith("["): continue # Ignore parameterset keys
            if only_stable_keys and not is_stable_key(key):
                continue
            if prefix is not None and not key.startswith(prefix): continue
            if isinstance(c[key], list):
                continue # Ignore list() as it is erroneous
            if c != config_layers[0]["config"] and isinstance(c[key], dict):
                continue # We do not consider parameter set. On the Builtin layer, we accept dict that contains metas
            if key not in k:
                k.append(key)
    return k

def dumps(only_stable_keys=True):
    c = {}
    for k in keys(only_stable_keys=only_stable_keys):
        c[k] = get_extended(k).copy()
        del c[k]["Success"]
    return c

def dump():
    builtin_layer = _init["all_configs"][0]
    r             = dumps(only_stable_keys=False)
    keys          = {}
    for k in r:
        key_info = r[k]
        if "Stable" not in key_info:
            continue
        if key_info["Stable"]:
            keys[k] = key_info
            continue

        if "WARNING" in key_info["Status"]:
            pattern_match = False
            for pattern in _init["ignored_warning_keys"]: 
                if re.match(pattern, k):
                    pattern_match = True
            if not pattern_match:
                log.warning(key_info["Status"])

        if k in builtin_layer and key_info["ConfigurationOrigin"] != builtin_layer["source"]:
            log.warning("Non STABLE key '%s' defined in '%s'! /!\ WARNING /!\ Its semantic and/or existence MAY change in future CloneSquad release!!"
                % (k, key_info["ConfigurationOrigin"]))
            keys[k] = key_info

    if get_int("config.dump_configuration"):
        log.info(Dbg.pprint(keys))
        log.info("Loaded files: %s " % [ x["source"] for x in _init["loaded_files"]])

def is_builtin_key_exist(key):
    builtin_layer = _init["all_configs"][0]["config"]
    return key in builtin_layer

def get_extended(key):
    active_parameter_set = _init["active_parameter_set"]
    stable_key           = is_stable_key(key)
    r = {
            "Key": key,
            "Value" : None,
            "Success" : False,
            "ConfigurationOrigin": "None",
            "Status": "[WARNING] Unknown configuration key '%s'" % key,
            "Stable": stable_key
    }
    builtin_layer = _init["all_configs"][0]["config"]

    key_def = None
    if key in builtin_layer and isinstance(builtin_layer[key], dict):
        key_def = builtin_layer[key]

    def _test_key(c):
        if key not in c or isinstance(c[key], list):
            return r
        if c != builtin_layer and isinstance(c[key], dict):
            return r
        pset_txt = " (ParameterSet='%s')" % parameter_set if parameter_set != "None" else ""
        res = {
                "Success": True,
                "ConfigurationOrigin" : config["source"],
                "Status": "Key found in '%s'%s" % (config["source"], pset_txt),
                "Stable": stable_key
            }
        res["Value"] = c[key]
        if key_def is not None:
            for k in key_def:
                res[k] = key_def[k]
            if c == builtin_layer:
                res["Value"] = key_def["DefaultValue"]
        r.update(res)
        if key not in builtin_layer:
            r["Status"] = "[WARNING] Key '%s' doesn't exist as built-in default (Misconfiguration??) but %s!" % (key, r["Status"])
        return r

    for config in _get_config_layers(reverse=True):
        c = config["config"]

        parameter_set = "None"
        if active_parameter_set in c:
            if key in c[active_parameter_set]:
                parameter_set = active_parameter_set
                r = _test_key(c[active_parameter_set])
                if r["Success"]: return r

        r = _test_key(c)
        if r["Success"]: return r

    return r

def set(key, value, ttl=None):
    if key == "config.active_parameter_set":
        _init["active_parameter_set"] = value if value != "" else None
        _parameterset_sanity_check()
    if ttl is None: ttl = get_duration_secs("config.default_ttl")
    _init["configuration_table"].set_kv(key, value, TTL=ttl)

def get_direct_from_kv(key, default=None):
    t = kvtable.KVTable(ctx, ctx["ConfigurationTable"])
    v = t.get_kv(key, direct=True)
    return v if not None else default

def import_dict(c):
    t = _init["configuration_table"]
    t.set_dict(c)

def get(key, none_on_failure=False):
    r = get_extended(key)
    if not r["Success"]:
        if none_on_failure:
            return None
        else:
            raise Exception(r["Status"])
    return str(r["Value"])

def get_int(key):
    return int(get(key))

def get_float(key):
    return float(get(key))

def get_list(key, separator=";", default=None):
    v = get(key)
    if v is None or v == "": return default
    return v.split(separator)

def get_duration_secs(key):
    try:
        return misc.str2duration_seconds(get(key))
    except Exception as e:
        raise Exception("[ERROR] Failed to parse config key '%s' as a duration! : %s" % (key, e))

def get_list_of_dict(key):
    v = get(key)
    if v is None: return []
    return misc.parse_line_as_list_of_dict(v)

def get_date(key, default=None):
    v = get(key)
    if v is None: return default
    return misc.str2utc(v, default=default)

def get_abs_or_percent(value_name, default, max_value):
    value = get(value_name)
    return misc.abs_or_percent(value, default, max_value)


