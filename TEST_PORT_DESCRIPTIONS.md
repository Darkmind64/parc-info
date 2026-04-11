# Test: Port Descriptions in Tooltips

## Problem Statement
Les descriptions configurées dans "Paramètres > Noms des services" n'apparaissaient pas dans les infobulles des badges de ports de l'inventaire des appareils.

## Root Cause
La fonction JavaScript `showTip()` utilisait une définition de port hardcodée (`PORTS_DEF`) au lieu de lire l'attribut `data-info` de l'élément survolé, qui contient la sortie complète du filtre `port_info` incluant la description configurée.

## Solution Applied

### 1. **Code Fix**: `templates/liste_appareils.html`

**Before:**
```javascript
function showTip(e, port) {
    const d = getPortDef(port);  // ← Hardcoded definitions
    const tip = document.getElementById('port-tip');
    document.getElementById('tip-port').textContent = d.icon + ' ' + port + ' — ' + d.label;
    document.getElementById('tip-info').textContent = d.info;  // ← Always hardcoded
    // ...
}
```

**After:**
```javascript
function showTip(e, port) {
    const element = e.currentTarget || e.target;  // ← Get the hovered element
    const d = getPortDef(port);
    const tip = document.getElementById('port-tip');

    // ← Read data-info attribute (contains configured description)
    const dataInfo = element.getAttribute('data-info') || d.info;

    document.getElementById('tip-port').textContent = d.icon + ' ' + port + ' — ' + d.label;
    document.getElementById('tip-info').textContent = dataInfo;  // ← Now uses configured description
    // ...
}
```

### 2. **How It Works**

#### Template Generation:
```html
<!-- In templates/liste_appareils.html -->
<span class="port-btn port-{{ p|port_class }} port-num-{{ p }}"
      data-info="{{ p|port_info }}"  <!-- This runs the port_info filter -->
      onmouseenter="showTip(event, {{ p }})">
    {{ p|port_icon }} {{ p|port_name }}
</span>
```

#### Port Info Filter (app.py):
```python
@app.template_filter('port_info')
def port_info_filter(port):
    try:
        port_int = int(port)
        cfg = get_port_config(port_int)
        name = cfg.get('name', str(port))
        desc = cfg.get('description', '')
        # Formater l'infobulle avec description si elle existe
        if desc:
            return f"{name} — {desc}"
        else:
            return f"{name} — Service TCP"
    except:
        return 'Port TCP ouvert'
```

#### Port Configuration Lookup (config_helpers.py):
```python
def get_port_config(port: int) -> dict:
    # ... setup ...
    desc_key = f'port_{port}_description'
    desc = cfg_get(desc_key) or ''  # ← Gets "Mon serveur SSH personnel" from DB
    # ...
    return {
        'port': port,
        'name': name,
        'description': desc,  # ← Returns custom description
        # ...
    }
```

### 3. **Data Flow Example**

Scenario: User configured `port_22_description = "Mon serveur SSH personnel"`

```
┌─────────────────────────────────────────────────────────────────┐
│ 1. Page Template Render (Jinja2)                                │
│    {{ 22|port_info }} ──→ port_info_filter(22) called           │
└─────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────┐
│ 2. port_info Filter (app.py)                                    │
│    get_port_config(22) ──→ retrieves 'Mon serveur SSH...'      │
│    Returns: "SSH — Mon serveur SSH personnel"                   │
└─────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────┐
│ 3. HTML Attribute                                               │
│    data-info="SSH — Mon serveur SSH personnel"                 │
└─────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────┐
│ 4. JavaScript Tooltip (showTip)                                 │
│    const dataInfo = element.getAttribute('data-info')          │
│    // dataInfo = "SSH — Mon serveur SSH personnel"             │
│    document.getElementById('tip-info').textContent = dataInfo   │
└─────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────┐
│ 5. User Sees Tooltip                                            │
│    ⌨ 22 — SSH                                                   │
│    SSH — Mon serveur SSH personnel                             │
│    (action information)                                         │
└─────────────────────────────────────────────────────────────────┘
```

## Test Procedure

1. **Start the app**: `python app.py`
2. **Navigate to**: Settings > Port Names (Paramètres > Noms des services)
3. **Configure a description**: Set `port_22_description = "Mon serveur SSH personnel"`
4. **Click Save**: Configuration is persisted to database
5. **Navigate to**: Inventory > Devices (Inventaire > Appareils)
6. **Hover over a port badge**: Port 22 badge should show:
   ```
   ⌨ 22 — SSH
   SSH — Mon serveur SSH personnel
   Cliquer pour ouvrir un terminal SSH
   ```

## Expected Result
✓ The custom description "Mon serveur SSH personnel" appears in the tooltip
✓ The description reflects the latest configuration from database
✓ Works for any port with a configured description

## Code Changed
- **File**: `templates/liste_appareils.html`
- **Function**: `showTip(e, port)`
- **Lines**: ~644-660
- **Change Type**: Read `data-info` attribute instead of using hardcoded definitions

## Verification
The fix is verified by:
1. Code review: showTip() now correctly reads from DOM element attributes
2. Template integration: data-info attribute correctly populated from port_info filter
3. Filter verification: port_info filter correctly formats name + description
4. Database layer: get_port_config() correctly retrieves descriptions from DB
