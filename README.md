# NetBox to Zabbix synchronization

A script to create, update and delete Zabbix hosts using NetBox device objects. Tested and compatible with all [currently supported Zabbix releases](https://www.zabbix.com/life_cycle_and_release_policy).

# Documentation

Documentation will be moved to the Github wiki of this project. Feel free to [check it out](https://github.com/TheNetworkGuy/netbox-zabbix-sync/wiki)!

## Installation via Docker

To pull the latest stable version to your local cache, use the following docker
pull command:

```bash
docker pull ghcr.io/thenetworkguy/netbox-zabbix-sync:main
```

Make sure to specify the needed environment variables for the script to work
(see [here](#set-environment-variables)) on the command line or use an
[env file](https://docs.docker.com/reference/cli/docker/container/run/#env).

```bash
docker run -d -t -i -e ZABBIX_HOST='https://zabbix.local' \ 
-e ZABBIX_TOKEN='othersecrettoken' \
-e NETBOX_HOST='https://netbox.local' \
-e NETBOX_TOKEN='secrettoken' \
--name netbox-zabbix-sync ghcr.io/thenetworkguy/netbox-zabbix-sync:main
```

This should run a one-time sync. You can check the sync with
`docker logs netbox-zabbix-sync`.

The image uses the default `config.py` for its configuration, you can use a
volume mount in the docker run command to override with your own config file if
needed (see [config file](#config-file)):

```bash
docker run -d -t -i -v $(pwd)/config.py:/opt/netbox-zabbix/config.py ...
```

## Installation from Source

### Cloning the repository

```bash
git clone https://github.com/TheNetworkGuy/netbox-zabbix-sync.git
```

### Development setup

Install the development dependencies and enable the formatting hook once per
clone:

```sh
uv sync --dev
uv run pre-commit install
```

Before each commit, the hook runs `uv run ruff format` across the repository.
If it reformats a file, review and stage the change, then commit again.

### Packages

Make sure that you have a python environment with the following packages
installed. You can also use the `requirements.txt` file for installation with
pip.

```sh
# Packages:
pynetbox
zabbix-utils

# Install them through requirements.txt from a venv:
virtualenv .venv
source .venv/bin/activate
.venv/bin/pip --require-virtualenv install -r requirements.txt
```

### Config file

First time user? Copy the `config.py.example` file to `config.py`. This file is
used for modifying filters and setting variables such as custom field names.

```sh
cp config.py.example config.py
```

### Set environment variables

Set the following environment variables:

```bash
ZABBIX_HOST="https://zabbix.local"
ZABBIX_USER="username"
ZABBIX_PASS="Password"
NETBOX_HOST="https://netbox.local"
NETBOX_TOKEN="secrettoken"
```

Or, you can use a Zabbix API token to login instead of using a username and
password. In that case `ZABBIX_USER` and `ZABBIX_PASS` will be ignored.

```bash
ZABBIX_TOKEN=othersecrettoken
```

If you are using custom SSL certificates for NetBox and/or Zabbix, you can set
the following environment variable to the path of your CA bundle file:

```sh
export REQUESTS_CA_BUNDLE=/path/to/your/ca-bundle.crt
```

### NetBox custom fields

Use the following custom fields in NetBox (if you are using config context for
the template information then the zabbix_template field is not required):

```
* Type: Integer
* Name: zabbix_hostid
* Required: False
* Default: null
* Object: dcim > device
```

If you use config-context split imports for OOB management, also create an OOB
host ID custom field. The field name is configurable with `oob_device_cf` and
defaults to `zabbix_oob_hostid`.

```
* Type: Integer
* Name: zabbix_oob_hostid
* Required: False
* Default: null
* Object: dcim > device
```

```
* Type: Text
* Name: zabbix_template
* Required: False
* Default: null
* Object: dcim > device_type
```

You can make the `zabbix_hostid` field hidden or read-only to prevent human
intervention.

This is optional, but there may be cases where you want to leave it
read-write in the UI. For example to manually change or clear the ID and re-run a sync.

## Virtual Machine (VM) Syncing

In order to use VM syncing, make sure that the `zabbix_id` custom field is also
present on Virtual machine objects in NetBox.

Use the `config.py` file and set the `sync_vms` variable to `True`.

You can set the `vm_hostgroup_format` variable to a customizable value for VM
hostgroups. The default is `cluster_type/cluster/role`.

To enable filtering for VM's, check the `nb_vm_filter` variable out. It works
the same as with the device filter (see documentation under "Hostgroup layout").
Note that not all filtering capabilities and properties of devices are
applicable to VM's and vice-versa. Check the NetBox API documentation to see
which filtering options are available for each object type.

## Unsynced object summary

At the end of every run, the syncer writes a warning-level summary of selected
NetBox devices, VMs, and OOB split imports that did not end up synced to
Zabbix. Entries are grouped by reason and list the affected object names.
Intentional exclusions, such as decommissioned objects and non-primary virtual
chassis members, are reported separately from sync failures. The summary covers
only objects returned by the configured NetBox filters.

## Azure Subscription Syncing

Azure subscriptions can be synced from NetBox Tenants. The sync only
processes Tenants with the configured Azure tag, which defaults to
`azure`.

Model each Azure subscription as a Tenant and set:

```
* Type: Integer
* Name: zabbix_hostid
* Required: False
* Default: null
* Object: tenancy > tenant
```

```
* Type: Text
* Name: azure_subscription_id
* Required: True
* Object: tenancy > tenant
```

The Tenant's Tenant Group represents the Azure management group/tenant
containing the subscriptions. Set this custom field on that Tenant Group:

```
* Type: Text
* Name: azure_tenant_id
* Required: True
* Object: tenancy > tenant group
```

Enable the feature and configure Zabbix Vault macro references for the service
principal credentials:

```python
sync_azure_subscriptions = True
azure_app_id_vault = "secret/azure:app_id"
azure_password_vault = "secret/azure:password"
```

Each synced Tenant is created as a Zabbix host named after the Tenant,
assigned to `Azure/Subscriptions`, linked to `Azure by HTTP`, and given
these macros:

- `{$AZURE.APP.ID}` from `azure_app_id_vault` as a Vault macro
- `{$AZURE.PASSWORD}` from `azure_password_vault` as a Vault macro
- `{$AZURE.SUBSCRIPTION.ID}` from `azure_subscription_id`
- `{$AZURE.TENANT.ID}` from the Tenant Group's `azure_tenant_id`

## Config file

### Hostgroup

Setting the `create_hostgroups` variable to `False` requires manual hostgroup
creation for devices in a new category. I would recommend setting this variable
to `True` since leaving it on `False` results in a lot of manual work.

The format can be set with the `hostgroup_format` variable for devices and
`vm_hostgroup_format` for virtual machines.

Any nested parent hostgroups will also be created automatically. For instance
the region `Berlin` with parent region `Germany` will create the hostgroup
`Germany/Berlin`.

Make sure that the Zabbix user has proper permissions to create hosts. The
hostgroups are in a nested format. This means that proper permissions only need
to be applied to the site name hostgroup and cascaded to any child hostgroups.

### Existing host adoption (ESXi-first)

You can let the syncer adopt existing Zabbix hosts when the NetBox host ID
custom field is empty. This is useful when hosts are pre-created by Zabbix
discovery (for example VMware LLD).

```python
adopt_existing_hosts = True
adopt_scope = "esxi"           # "esxi", "azure", "cloud", or "all"
adopt_for_vms = True           # include VMs in adoption scope checks
adopt_enrich_mode = "full"     # "full" or "metadata_only"
```

Behavior:

- Adoption is attempted only for objects in scope.
- `adopt_scope = "esxi"` matches objects where NetBox `platform` contains
  `ESXi` (case-insensitive).
- `adopt_scope = "azure"` matches VMs where the configured Azure resource ID
  field is populated, or where the VM platform, cluster, cluster type, tenant,
  or tag contains one of `azure_vm_platform_keywords` (default: `["azure"]`).
- A unique name match in Zabbix is required. If multiple hosts match, adoption
  is skipped for safety.
- LLD-created hosts (including VMware-discovered VMs) retain discovery-owned
  fields: technical and visible names, LLD-linked templates, prototype groups,
  automatic tags/macros, and scalar host settings. NetBox still reconciles
  manually linked templates, groups, tags, and macros without removing the
  discovery-owned entries.
- Azure VM hosts linked to `azure_vm_discovered_templates` (default:
  `["Azure Virtual Machine by HTTP"]`) continue to use metadata-only
  enrichment after adoption.
- On successful adoption, the script writes the matched `hostid` into the
  configured NetBox custom field (`device_cf`, or `oob_device_cf` for OOB
  split imports).

For Azure VM adoption from the `Azure by HTTP` discovery, use:

```python
adopt_existing_hosts = True
adopt_scope = "azure"
adopt_for_vms = True
adopt_enrich_mode = "metadata_only"
azure_vm_platform_keywords = ["azure"]
azure_vm_discovered_templates = ["Azure Virtual Machine by HTTP"]
azure_vm_resource_id_cf = ""   # optional NetBox VM custom field
```

`adopt_enrich_mode` controls post-adoption sync:

- `full`: keep normal consistency checks (templates, groups, status, proxy,
  interfaces, inventory, usermacros, tags).
- `metadata_only`: sync metadata-oriented fields only (inventory mode/inventory,
  usermacros, tags), and skip template/group/status/proxy/interface changes.

#### Layout

The default hostgroup layout is "site/manufacturer/device_role". You can change
this behaviour with the hostgroup_format variable. The following values can be
used:

**Both devices and virtual machines**

| name          | description                                                                          |
| ------------- | ------------------------------------------------------------------------------------ |
| role          | Role name of a device or VM                                                          |
| region        | The region name                                                                      |
| site          | Site name                                                                            |
| site_group    | Site group name                                                                      |
| tenant        | Tenant name                                                                          |
| tenant_group  | Tenant group name                                                                    |
| platform      | Software platform of a device or VM                                                  |
| owner         | Assigned owner name (requires NetBox 4.5 or newer)                                   |
| owner_group   | Group of the assigned owner (requires NetBox 4.5 or newer)                           |
| custom fields | See the section "Layout -> Custom Fields" to use custom fields as hostgroup variable |

**Only for devices**

| name         | description              |
| ------------ | ------------------------ |
| device_type  | Device type model        |
| location     | The device location name |
| manufacturer | Device manufacturer name |
| rack         | Rack                     |

**Only for VMs**

| name         | description      |
| ------------ | ---------------  |
| cluster      | VM cluster name  |
| cluster_type | VM cluster type  |
| device       | parent device    |

You can specify the value separated by a "/" like so:

```python
hostgroup_format = "tenant/site/location/role"
```

For example, manufacturer and device type can be separate group levels:

```python
hostgroup_format = "site/manufacturer/device_type/role"
```

You can also provice a list of groups like so:

```python
hostgroup_format = ["region/site_group/site", "role", "tenant_group/tenant"]
```

**Group traversal**

The default behaviour for `region` is to only use the directly assigned region
in the rendered hostgroup name. However, by setting `traverse_region` to `True`
in `config.py` the script will render a full region path of all parent regions
for the hostgroup name. `traverse_site_groups` controls the same behaviour for
site_groups.

**Hardcoded text**

You can add hardcoded text in the hostgroup format by using quotes, this will
insert the literal text:

```python
hostgroup_format = "'MyDevices'/location/role"
```

In this case, the prefix MyDevices will be used for all generated groups.

**Custom fields**

You can use the value of custom fields for hostgroup generation. This allows
more freedom and even allows a full static mapping instead of a dynamic rendered
hostgroup name.

For instance a custom field with the name `mycustomfieldname` and type string
has the following values for 2 devices:

```
Device A has the value Train for custom field mycustomfieldname.
Device B has the value Bus for custom field mycustomfieldname.
Both devices are located in the site Paris.
```

With the hostgroup format `site/mycustomfieldname` the following hostgroups will
be generated:

```
Device A: Paris/Train
Device B: Paris/Bus
```

**Empty variables or hostgroups**

Should the content of a variable be empty, then the hostgroup position is
skipped.

For example, consider the following scenario with 2 devices, both the same
device type and site. One of them is linked to a tenant, the other one does not
have a relationship with a tenant.

- Device_role: PDU
- Site: HQ-AMS

```python
hostgroup_format = "site/tenant/role"
```

When running the script like above, the following hostgroup (HG) will be
generated for both hosts:

- Device A with no relationship with a tenant: HQ-AMS/PDU
- Device B with a relationship to tenant "Fork Industries": HQ-AMS/Fork
  Industries/PDU

The same logic applies to custom fields being used in the HG format:

```python
hostgroup_format = "site/mycustomfieldname"
```

For device A with the value "ABC123" in the custom field "mycustomfieldname" ->
HQ-AMS/ABC123 For a device which does not have a value in the custom field
"mycustomfieldname" -> HQ-AMS

Should there be a scenario where a custom field does not have a value under a
device, and the HG format only uses this single variable, then this will result
in an error:

```
hostgroup_format = "mycustomfieldname"

NetBox-Zabbix-sync - ERROR - ESXI1 has no reliable hostgroup. This is most likely due to the use of custom fields that are empty.
```

### Extended site properties

By default, NetBox will only return the following properties under the 'site' key for a device:

- site id
- (api) url
- display name
- name
- slug
- description

However, NetBox-Zabbix-Sync allows you to extend these site properties with the full site information
so you can use this data in inventory fields, tags and usermacros.

To enable this functionality, enable the following setting in your configuration file:

`extended_site_properties = True`

Keep in mind that enabling this option will increase the number of API calls to your NetBox instance,
this might impact performance on large syncs.

### Device status

By setting a status on a NetBox device you determine how the host is added (or
updated) in Zabbix. There are, by default, 3 options:

- Delete the host from Zabbix (triggered by NetBox status "Decommissioning" and
  "Inventory")
- Create the host in Zabbix but with a disabled status (Trigger by "Offline",
  "Planned", "Staged" and "Failed")
- Create the host in Zabbix with an enabled status (For now only enabled with
  the "Active" status)

You can modify this behaviour by changing the following list variables in the
script:

- `zabbix_device_removal`
- `zabbix_device_disable`

### Zabbix Inventory

This script allows you to enable the inventory on managed Zabbix hosts and sync
NetBox device properties to the specified inventory fields. To map NetBox
information to NetBox inventory fields, set `inventory_sync` to `True`.

You can set the inventory mode to "disabled", "manual" or "automatic" with the
`inventory_mode` variable. See
[Zabbix Manual](https://www.zabbix.com/documentation/current/en/manual/config/hosts/inventory#building-inventory)
for more information about the modes.

Use the `device_inventory_map` variable to map which NetBox properties are used in
which Zabbix Inventory fields. For nested properties, you can use the '/'
seperator. For example, the following map will assign the custom field
'mycustomfield' to the 'alias' Zabbix inventory field:

For Virtual Machines, use `vm_inventory_map`.

```python
inventory_sync = True
inventory_mode = "manual"
device_inventory_map = {"custom_fields/mycustomfield": "alias"}
vm_inventory_map = {"custom_fields/mycustomfield": "alias"}
```

See `config.py.example` for an extensive example map. Any Zabbix Inventory fields
that are not included in the map will not be touched by the script, so you can
safely add manual values or use items to automatically add values to other
fields.

### Template source

You can either use a NetBox device type custom field or NetBox config context
for the Zabbix template information.

Using a custom field allows for only one template. You can assign multiple
templates to one host using the config context source. Should you make use of an
advanced templating structure with lots of nesting then i would recommend
sticking to the custom field.

You can change the behaviour in the config file. By default this setting is
false but you can set it to true to use config context:

```python
templates_config_context = True
```

After that make sure that for each host there is at least one template defined
in the config context in this format:

```json
{
    "zabbix": {
        "templates": [
            "TemplateA",
            "TemplateB",
            "TemplateC",
            "Template123"
        ]
    }
}
```

You can also opt for the default device type custom field behaviour but with the
added benefit of overwriting the template should a device in NetBox have a
device specific context defined. In this case the device specific context
template(s) will take priority over the device type custom field template.

```python
templates_config_context_overrule = True
```

### OOB device import

By default one NetBox device is imported as one Zabbix host using the device's
`primary_ip`. You can also import the device's `oob_ip` as a second Zabbix host
by adding an `oob` node under `zabbix` in config context.

The normal `zabbix` node continues to configure the primary host. The nested
`zabbix.oob` node configures the OOB host and can use the same fields as the
primary host, such as `templates`, `interface_type`, `interface_port`, `snmp`,
`proxy`, `proxy_group`, `tags`, `usermacros`, and `description`.

The OOB host name is derived from the primary host name. Use `name_prefix` or
`name_suffix` in `zabbix.oob`; if neither is set, the suffix `-oob` is used. The
OOB host ID is stored in the custom field configured by `oob_device_cf`, which
defaults to `zabbix_oob_hostid`.

```json
{
    "zabbix": {
        "templates": ["Template Module ICMP Ping"],
        "oob": {
            "name_prefix": "drac-",
            "templates": [" Dell iDRAC by SNMP"],
            "interface_type": 2,
            "snmp": {
                "version": 2,
                "community": "{$SNMP_COMMUNITY}"
            }
        }
    }
}
```

With a NetBox device named `vmhost1`, this example creates an OOB Zabbix
host named `drac-vmhost1`.

### Tags

This script can sync host tags to your Zabbix hosts for use in filtering,
SLA calculations and event correlation.

Tags can be synced from the following sources:

1. NetBox device/vm tags
2. NetBox config context
3. NetBox fields

Syncing tags will override any tags that were set manually on the host,
making NetBox the single source-of-truth for managing tags.

To enable syncing, turn on `tag_sync` in the config file.
By default, this script will modify tag names and tag values to lowercase.
You can change this behavior by setting `tag_lower` to `False`.

```python
tag_sync = True
tag_lower = True
```

#### Device tags

As NetBox doesn't follow the tag/value pattern for tags, we will need a tag
name set to register the netbox tags.

By default the tag name is "NetBox", but you can change this to whatever you want.
The value for the tag can be set to 'name', 'display', or 'slug', which refers to the
property of the NetBox tag object that will be used as the value in Zabbix.

```python
tag_name = 'NetBox'
tag_value = 'name'
```

#### Config context

You can supply custom tags via config context by adding the following:

```json
{
    "zabbix": {
        "tags": [
            {
                "MyTagName": "MyTagValue"
            },
            {
                "environment": "production"
            }
        ],
    }
}
```

This will allow you to assign tags based on the config context rules.

#### NetBox Field

NetBox field can also be used as input for tags, just like inventory and usermacros.
To enable syncing from fields, make sure to configure a `device_tag_map` and/or a `vm_tag_map`.

```python
device_tag_map = {"site/name": "site",
                  "rack/name": "rack",
                  "platform/name": "target"}

vm_tag_map = {"site/name": "site",
              "cluster/name": "cluster",
              "platform/name": "target"}
```

To turn off field syncing, set the maps to empty dictionaries:

```python
device_tag_map = {}
vm_tag_map = {}
```

### Usermacros

You can choose to use NetBox as a source for Host usermacros by
enabling the following option in the configuration file:

```python
usermacro_sync = True
```

Please be advised that `usermacro_sync = True` makes NetBox own the full
host usermacro list. It will _clear_ any usermacros manually set on the
managed hosts and override them with the usermacros from NetBox.

To let NetBox manage only the usermacros it defines while preserving
Zabbix-only usermacros, use partial sync:

```python
usermacro_sync = "partial"
```

In partial mode, NetBox usermacros from both config context and field maps
overwrite existing Zabbix usermacros with the same name. Usermacros that exist
only in Zabbix are left unchanged.

There are two NetBox sources that can be used to populate usermacros:

1. NetBox config context
2. NetBox fields

#### Config context

By defining a dictionary `usermacros` within the `zabbix` key in
config context, you can dynamically assign usermacro values based on
anything that you can target based on
[config contexts](https://netboxlabs.com/docs/netbox/en/stable/features/context-data/)
within NetBox.

Through this method, it is possible to define the following types of usermacros:

1. Text
2. Secret
3. Vault

The default macro type is text, if no `type` and `value` have been set.
It is also possible to create usermacros with
[context](https://www.zabbix.com/documentation/7.0/en/manual/config/macros/user_macros_context).

Examples:

```json
{
    "zabbix": {
        "usermacros": {
            "{$USER_MACRO}": "test value",
            "{$CONTEXT_MACRO:\"test\"}": "test value",
            "{$CONTEXT_REGEX_MACRO:regex:\".*\"}": "test value",
            "{$SECRET_MACRO}": {
                "type": "secret",
                "value": "PaSsPhRaSe"
            },
            "{$VAULT_MACRO}": {
                "type": "vault",
                "value": "secret/vmware:password"
            },
            "{$USER_MACRO2}": {
                "type": "text",
                "value": "another test value"
            },
            "{$VMWARE.URL}": {
                "type": "text",
                "value": "https://{netbox:name}/sdk"
            }
        }
    }
}

```

Config context usermacro values can expand data from the NetBox object being
synced by using `{netbox:<path>}` placeholders. The path uses the same `/`
separator as other NetBox field maps. For example, `{netbox:name}` resolves to
the device or VM name and `{netbox:custom_fields/vmware_fqdn}` resolves to a
custom field value.

Please be aware that secret usermacros are only synced _once_ by default.
This is the default behavior because Zabbix API won't return the value of
secrets so the script cannot compare the values with those set in NetBox.

If you update a secret usermacro value, just remove the value from the host
in Zabbix and the new value will be synced during the next run.

Alternatively, you can set the following option in the config file:

```python
usermacro_sync = "full"
```

This keeps full-list ownership and forces secret usermacro values to be sent on
every run. That way, you will know for sure the secret values are always up to
date.

Keep in mind that NetBox will show your secrets in plain text.
If true secrecy is required, consider switching to
[vault](https://www.zabbix.com/documentation/current/en/manual/config/macros/secret_macros#vault-secret)
usermacros.

#### Netbox Fields

To use NetBox fields as a source for usermacros, you will need to set up usermacro maps
for devices and/or virtual machines in the configuration file.
This method only supports `text` type usermacros.

For example:

```python
usermacro_sync = True
device_usermacro_map = {"serial": "{$HW_SERIAL}",
                        "role/name": "{$DEV_ROLE}", 
                        "url": "{$NB_URL}",
                        "id": "{$NB_ID}"}
vm_usermacro_map = {"memory": "{$TOTAL_MEMORY}",
                    "role/name": "{$DEV_ROLE}", 
                    "url": "{$NB_URL}",
                    "id": "{$NB_ID}"}
```

### TLS encryption

You can sync the Zabbix host encryption (TLS) settings so that synced hosts use
certificate or PSK based encryption instead of the default "No encryption".

Syncing is disabled by default. Enable it in the config file:

```python
tls_sync = True
```

While `tls_sync` is `False`, no encryption settings are pushed or reconciled and
the Zabbix defaults are left untouched.

The settings can be defined globally in the config file and overruled per host
through the NetBox config context. The available settings are:

- `tls_connect`: how Zabbix connects to the host. One of `none`, `psk`, `cert`.
- `tls_accept`: which connections the host accepts. A list combining `none`,
  `psk` and/or `cert`.
- `tls_issuer`, `tls_subject`: certificate issuer/subject (`cert` mode, optional).
- `tls_psk_identity`, `tls_psk`: PSK identity and key (required for `psk` mode).
  `tls_psk` is a secret and is masked in the log output.

#### Global config

```python
tls_sync = True
tls_connect = "cert"
tls_accept = ["cert"]
tls_issuer = "CN=My CA"
tls_subject = "CN=host1.example.com"
```

#### Config context

Per host, the global defaults can be overruled by adding the matching keys under
the `zabbix` key in the NetBox config context:

```json
{
    "zabbix": {
        "tls_connect": "cert",
        "tls_accept": ["cert"],
        "tls_issuer": "CN=My CA",
        "tls_subject": "CN=host1.example.com"
    }
}
```

A PSK example:

```json
{
    "zabbix": {
        "tls_connect": "psk",
        "tls_accept": ["psk"],
        "tls_psk_identity": "PSK 001",
        "tls_psk": "16-or-more-hex-character-key"
    }
}
```

## Permissions

### NetBox

Make sure that the NetBox user has proper permissions for device read and modify
(modify to set the Zabbix HostID custom field) operations. The user should also
have read-only access to the device types.

### Zabbix

Make sure that the Zabbix user has permissions to read hostgroups and proxy
servers. The user should have full rights on creating, modifying and deleting
hosts.

If you want to automatically create hostgroups then the create permission on
host-groups should also be applied.

### Custom links

To make the user experience easier you could add a custom link that redirects
users to the Zabbix latest data.

```
* Name: zabbix_latestData
* Text: {% if object.cf["zabbix_hostid"] %}Show host in Zabbix{% endif %}
* URL: http://myzabbixserver.local/zabbix.php?action=latest.view&hostids[]={{ object.cf["zabbix_hostid"] }}
```

## Running the script

```
python3 netbox_zabbix_sync.py
```

### Flags

| Flag | Option    | Description                           |
| ---- | --------- | ------------------------------------- |
| -v   | verbose   | Log with info on.                     |
| -vv  | debug     | Log with debugging on.                |
| -vvv | debug-all | Log with debugging on for all modules |

## Config context

### Zabbix proxy

#### Config Context

You can set the proxy for a device using the `proxy` key in config context.

```json
{
    "zabbix": {
        "proxy": "yourawesomeproxy.local"
    }
}
```

It is now possible to specify proxy groups with the introduction of Proxy groups
in Zabbix 7. Specifying a group in the config context on older Zabbix releases
will have no impact and the script will ignore the statement.

```json
{
    "zabbix": {
        "proxy_group": "yourawesomeproxygroup.local"
    }
}
```

The script will prefer groups when specifying both a proxy and group. This is
done with the assumption that groups are more resilient and HA ready, making it
a more logical choice to use for proxy linkage. This also makes migrating from a
proxy to proxy group easier since the group take priority over the individual
proxy.

```json
{
    "zabbix": {
        "proxy": "yourawesomeproxy.local",
        "proxy_group": "yourawesomeproxygroup.local"
    }
}
```

In the example above the host will use the group on Zabbix 7. On Zabbix 6 and
below the host will use the proxy. Zabbix 7 will use the proxy value when
omitting the proxy_group value.

#### Custom Field

Alternatively, you can use a custom field for assigning a device or VM to
a Zabbix proxy or proxy group. The custom fields can be assigned to both
Devices and VMs.

You can also assign these custom fields to a site to allow all devices/VMs
in that site to be configured with the same proxy or proxy group.
In order for this to work, `extended_site_properties` needs to be enabled in
the configuration as well.

To use the custom fields for proxy configuration, configure one or both
of the following settings in the configuration file with the actual names of your
custom fields:

```python
proxy_cf = "zabbix_proxy"
proxy_group_cf = "zabbix_proxy_group"
```

As with config context proxy configuration, proxy group will take precedence over
standalone proxy when configured.
Proxy settings configured on the device or VM will in their turn take precedence
over any site configuration.

If the custom fields have no value but the proxy or proxy group is configured in config context,
that setting will be used.

### Set interface parameters within NetBox

When adding a new device, you can set the interface type with custom context. By
default, the following configuration is applied when no config context is
provided:

- SNMPv2
- UDP 161
- Bulk requests enabled
- SNMP community: {$SNMP_COMMUNITY}

Due to Zabbix limitations of changing interface type with a linked template,
changing the interface type from within NetBox is not supported and the script
will generate an error.

For example, when changing a SNMP interface to an Agent interface:

```
NetBox-Zabbix-sync - WARNING - Device: Interface OUT of sync.
NetBox-Zabbix-sync - ERROR - Device: changing interface type to 1 is not supported.
```

To configure the interface parameters you'll need to use custom context. Custom
context was used to make this script as customizable as possible for each
environment. For example, you could:

- Set the custom context directly on a device
- Set the custom context on a tag, which you would add to a device (for
  instance, SNMPv3)
- Set the custom context on a device role
- Set the custom context on a site or region

##### Agent interface configuration example

```json
{
    "zabbix": {
        "interface_port": 1500,
        "interface_type": 1
    }
}
```

##### SNMPv2 interface configuration example

```json
{
    "zabbix": {
        "interface_port": 161,
        "interface_type": 2,
        "snmp": {
            "bulk": 1,
            "community": "SecretCommunity",
            "version": 2
        }
    }
}
```

##### SNMPv3 interface configuration example

```json
{
    "zabbix": {
        "interface_port": 1610,
        "interface_type": 2,
        "snmp": {
            "authpassphrase": "SecretAuth",
            "bulk": 1,
            "securitylevel": 1,
            "securityname": "MySecurityName",
            "version": 3
        }
    }
}
```

I would recommend using usermacros for sensitive data such as community strings
since the data in NetBox is plain-text.

> **_NOTE:_** Not all SNMP data is required for a working configuration.
> [The following parameters are allowed](https://www.zabbix.com/documentation/current/manual/api/reference/hostinterface/object#details_tag "The following parameters are allowed") but
> are not all required, depending on your environment.
