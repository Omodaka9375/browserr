"""Page-side JavaScript used by the Driver, as pure data.

Split out of driver.py: these are the only large string blocks in the
SDK, and they change with page semantics, not transport semantics.
driver.py binds them as class attributes, so Driver.READ_JS etc. and
self.READ_JS keep working unchanged."""
READ_JS = r"""(() => {
    // Text extraction with Shadow DOM piercing (open roots): a host
    // element's rendered content is its shadowRoot's children - light-DOM
    // children only render through <slot>, which is expanded to its
    // assigned nodes (fallback children when nothing is assigned).
    // read_structure pierces shadows; read_page must see the same DOM.
    const pick = (el) => {
        // Skip invisible branches entirely.
        const st = getComputedStyle(el);
        if (st.display === 'none' || st.visibility === 'hidden') return [];
        const out = [];
        const kids = el.shadowRoot ? el.shadowRoot.childNodes : el.childNodes;
        for (const raw of kids) {
            let nodes = [raw];
            if (raw.nodeType === 1 && raw.tagName === 'SLOT') {
                const a = raw.assignedNodes ? raw.assignedNodes() : [];
                nodes = a.length ? a : Array.from(raw.childNodes);
            }
            for (const n of nodes) {
                if (n.nodeType === 3) {
                    const t = n.textContent.replace(/\s+/g, ' ').trim();
                    if (t) out.push(t);
                } else if (n.nodeType === 1) {
                    const tag = n.tagName;
                    if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT' || tag === 'TEMPLATE') continue;
                    const inner = pick(n);
                    if (tag === 'A' && inner.length) {
                        out.push('[' + inner.join(' ') + '](' + n.href + ')');
                    } else if ((tag === 'P' || tag === 'LI' || tag === 'H1' || tag === 'H2' || tag === 'H3' || tag === 'H4' || tag === 'BLOCKQUOTE') && inner.length) {
                        out.push('\n\n' + inner.join(' '));
                    } else {
                        out.push(...inner);
                    }
                }
            }
        }
        return out;
    };
    return [document.title, pick(document.body).join(' ')];
})()"""

LINKS_JS = r"""(() => {
    const out = [];
    for (const a of document.links) {
        const r = a.getBoundingClientRect();
        if (!r.width || !r.height) continue;
        const text = a.innerText.replace(/\s+/g, ' ').trim() || a.href;
        out.push({text: text, href: a.href, x: r.x, y: r.y, w: r.width, h: r.height});
    }
    return out;
})()"""

FORMS_JS = r"""(() => {
    const visible = (el) => {
        const st = getComputedStyle(el);
        return st.display !== 'none' && st.visibility !== 'hidden';
    };
    const fields = (form) => {
        const out = [];
        for (const i of form.querySelectorAll('input, textarea, select')) {
            if (!visible(i)) continue;
            const r = i.getBoundingClientRect();
            out.push({
                tag: i.tagName.toLowerCase(), type: i.type || '',
                name: i.name || '', id: i.id || '',
                placeholder: i.placeholder || '',
                value: i.value || '',
                x: r.x, y: r.y, w: r.width, h: r.height,
            });
        }
        return out;
    };
    const out = [];
    for (const f of document.forms) {
        const fs = fields(f);
        if (!fs.length) continue;
        out.push({action: f.action || location.href, method: f.method, fields: fs});
    }
    // Password fields outside any <form> (common in SPA logins).
    for (const p of document.querySelectorAll('input[type=password]')) {
        if (!p.closest('form') && visible(p)) {
            const r = p.getBoundingClientRect();
            out.push({action: location.href, method: 'none', fields: [{
                tag: 'input', type: 'password', name: p.name || '', id: p.id || '',
                placeholder: p.placeholder || '', value: '',
                x: r.x, y: r.y, w: r.width, h: r.height,
            }]});
        }
    }
    return out;
})()"""

STRUCTURE_JS = r"""(() => {
    // Structured page inventory: interactive elements + headings, with
    // rects, a usable CSS selector and a STABLE REF (e1, e2, ...) that
    // an agent and a human can both point at. Form fields also carry
    // their label, constraints, current state and computed validity.
    // Shadow DOM is pierced (querySelector does not cross shadowRoot).
    const sel = (el) => {
        if (el.id) return '#' + CSS.escape(el.id);
        const name = el.getAttribute('name');
        if (name) return `${el.tagName.toLowerCase()}[name="${CSS.escape(name)}"]`;
        // Nth-of-type path from <body>; unique in practice for the
        // pages agents drive.
        const parts = [];
        let n = el;
        while (n && n.tagName !== 'BODY' && n.parentElement) {
            const sibs = [...n.parentElement.children].filter(
                c2 => c2.tagName === n.tagName);
            const i = sibs.indexOf(n) + 1;
            parts.unshift(n.tagName.toLowerCase()
                + (sibs.length > 1 ? `:nth-of-type(${i})` : ''));
            n = n.parentElement;
        }
        return parts.join(' > ');
    };
    const rect = (el) => {
        const r = el.getBoundingClientRect();
        return {x: r.x, y: r.y, w: r.width, h: r.height};
    };
    const txt = (n, max) => ((n && n.textContent) || '')
        .replace(/\s+/g, ' ').trim().slice(0, max);
    const vis = (el) => {
        const s = getComputedStyle(el);
        if (s.display === 'none' || s.visibility === 'hidden') return false;
        if (parseFloat(s.opacity) === 0) return false;
        return true;
    };

    // Label resolution: aria-labelledby, aria-label, <label for>,
    // wrapping <label>, then placeholder/title as a last resort.
    const labelOf = (el) => {
        const ids = el.getAttribute('aria-labelledby');
        if (ids) {
            const t = ids.split(/\s+/).map(id => document.getElementById(id))
                .filter(Boolean).map(n => txt(n, 120)).join(' ').trim();
            if (t) return t;
        }
        const al = el.getAttribute('aria-label');
        if (al && al.trim()) return al.trim().slice(0, 120);
        if (el.id) {
            const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
            if (l) { const t = txt(l, 120); if (t) return t; }
        }
        const wrap = el.closest ? el.closest('label') : null;
        if (wrap) { const t = txt(wrap, 120); if (t) return t; }
        if (el.tagName === 'INPUT'
            && (el.type === 'submit' || el.type === 'button')) return el.value || '';
        return (el.getAttribute('placeholder') || el.getAttribute('title') || '').slice(0, 120);
    };

    // Constraints read from attributes: servo 0.8's constraint API is
    // partial, so validity is computed here instead.
    const constraintsOf = (el) => {
        const c = {};
        if (el.hasAttribute('required')
            || el.getAttribute('aria-required') === 'true') c.required = true;
        for (const a of ['pattern', 'minlength', 'maxlength', 'min', 'max',
                         'step', 'autocomplete', 'inputmode']) {
            const v = el.getAttribute(a);
            if (v !== null && v !== '') c[a] = v;
        }
        return c;
    };
    const validityOf = (el, c) => {
        const reasons = [];
        const type = (el.type || '').toLowerCase();
        const val = String(el.value == null ? '' : el.value);
        if (!el.disabled && !el.readOnly) {
            if (c.required) {
                const empty = (type === 'checkbox' || type === 'radio')
                    ? !el.checked : val.trim() === '';
                if (empty) reasons.push('required');
            }
            if (c.pattern && val) {
                try {
                    if (!new RegExp('^(?:' + c.pattern + ')$').test(val)) reasons.push('pattern');
                } catch (_) {}
            }
            if (c.maxlength && val.length > +c.maxlength) reasons.push('maxlength');
            if (c.minlength && val && val.length < +c.minlength) reasons.push('minlength');
            if (type === 'email' && val
                && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(val)) reasons.push('email');
            if (type === 'url' && val) {
                try { new URL(val); } catch (_) { reasons.push('url'); }
            }
            if ((type === 'number' || type === 'range') && val) {
                const n = parseFloat(val);
                if (!isNaN(n)) {
                    if (c.min !== undefined && n < parseFloat(c.min)) reasons.push('min');
                    if (c.max !== undefined && n > parseFloat(c.max)) reasons.push('max');
                }
            }
        }
        return reasons;
    };

    // One deep pass: every element, shadow roots included (depth-capped).
    const all = [];
    const walk = (root, depth) => {
        if (depth > 8) return;
        for (const el of root.querySelectorAll('*')) {
            all.push(el);
            if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
        }
    };
    walk(document, 0);

    // Fresh refs each read: a ref from an older page resolves to
    // nothing, so callers get a clean 'not found'.
    const refs = {};
    window.__browserr_refs = refs;
    let seq = 0;
    const ref = (el) => {
        seq += 1;
        const r = 'e' + seq;
        refs[r] = el;
        return r;
    };

    const out = {title: document.title, url: location.href,
                 headings: [], buttons: [], inputs: [], selects: [],
                 links: [], images: [], forms: []};
    const formIndex = new Map();
    const formOf = (f) => {
        if (!formIndex.has(f)) {
            formIndex.set(f, out.forms.length);
            out.forms.push({id: f.id || '', name: f.name || '',
                            action: f.action || '',
                            method: (f.method || 'get').toLowerCase(),
                            enctype: f.enctype || '', fields: [], submits: []});
        }
        return out.forms[formIndex.get(f)];
    };

    const field = (el) => {
        const tag = el.tagName.toLowerCase();
        let type = (el.type || 'text').toLowerCase();
        if (tag === 'textarea') type = 'textarea';
        if (el.isContentEditable && tag !== 'textarea' && tag !== 'input') type = 'richtext';
        const c = constraintsOf(el);
        const reasons = validityOf(el, c);
        const o = {
            ref: ref(el), tag: tag, type: type,
            name: el.name || '', id: el.id || '',
            label: labelOf(el),
            placeholder: el.placeholder || '',
            selector: sel(el), ...rect(el),
            disabled: !!el.disabled, readonly: !!el.readOnly,
            focused: document.activeElement === el,
        };
        if (type === 'checkbox' || type === 'radio') {
            o.checked = !!el.checked;
            if (type === 'radio' && el.name) o.group = el.name;
        } else if (el.isContentEditable) {
            const t = ((el.textContent || '')).replace(/\\s+/g, ' ').trim();
            o.value = t.slice(0, 200);
            if (t.length > 200) o.value_clipped = t.length;
        } else {
            const v = String(el.value == null ? '' : el.value);
            o.value = v.slice(0, 200);
            if (v.length > 200) o.value_clipped = v.length;
        }
        // JS numbers arrive as doubles through eval; keep the JSON int.
        if (o.value_clipped !== undefined) o.value_clipped = Math.round(o.value_clipped);
        if (Object.keys(c).length) o.constraints = c;
        o.valid = reasons.length === 0;
        if (reasons.length) o.invalid = reasons;
        const f = el.form;
        if (f) {
            const meta = formOf(f);
            meta.fields.push(o.ref);
            o.form = {id: meta.id, action: meta.action, method: meta.method};
        }
        return o;
    };

    for (const el of all) {
        if (/^H[1-6]$/.test(el.tagName) && vis(el)) {
            out.headings.push({level: +el.tagName[1], text: txt(el, 200)});
        }
    }
    for (const el of all) {
        const tag = el.tagName;
        const isBtn = tag === 'BUTTON'
            || el.getAttribute('role') === 'button'
            || (tag === 'INPUT'
                && ['submit', 'button', 'reset', 'image'].includes((el.type || '').toLowerCase()));
        if (!isBtn || !vis(el)) continue;
        const r = rect(el);
        if (!r.w && !r.h) continue;
        const o = {ref: ref(el), text: txt(el, 80) || (el.value || ''),
                   type: (el.type || 'button').toLowerCase(),
                   selector: sel(el), ...r, disabled: !!el.disabled};
        out.buttons.push(o);
        const f = el.form;
        if (f) formOf(f).submits.push(o.ref);
    }
    for (const el of all) {
        const tag = el.tagName;
        const type = (el.type || '').toLowerCase();
        const skip = tag === 'INPUT'
            && ['submit', 'button', 'reset', 'image', 'hidden'].includes(type);
        const isField = (tag === 'INPUT' && !skip) || tag === 'TEXTAREA'
            || (el.isContentEditable && tag !== 'textarea' && tag !== 'input');
        if (!isField || !vis(el)) continue;
        const r = rect(el);
        if (!r.w && !r.h) continue;
        out.inputs.push(field(el));
    }
    for (const el of all) {
        if (el.tagName !== 'SELECT' || !vis(el)) continue;
        const o = field(el);
        o.multiple = !!el.multiple;
        o.options = [...el.options].slice(0, 80).map(x => ({
            value: x.value, label: txt(x, 60),
            disabled: !!x.disabled, selected: !!x.selected}));
        out.selects.push(o);
    }
    for (const a of document.links) {
        const r = a.getBoundingClientRect();
        if (!r.width || !r.height) continue;
        const text = a.innerText.replace(/\s+/g, ' ').trim() || a.href;
        out.links.push({text: text.slice(0, 80), href: a.href,
                        x: r.x, y: r.y, w: r.width, h: r.height});
    }
    for (const im of document.images) {
        const r = im.getBoundingClientRect();
        if (!r.width || !r.height) continue;
        out.images.push({alt: (im.alt || '').slice(0, 80), src: im.src.slice(0, 200),
                         x: r.x, y: r.y, w: r.width, h: r.height});
    }
    return out;
})()"""

REF_RECT_JS = r"""(() => {
    // Resolve a stable ref from the last read_structure() to a click
    // point. Stale refs (page navigated since) resolve to null.
    const el = (window.__browserr_refs || {})[%s];
    if (!el || !el.isConnected) return null;
    el.scrollIntoView({block: 'center', inline: 'center'});
    const r = el.getBoundingClientRect();
    return {x: r.x + r.width / 2, y: r.y + r.height / 2,
            w: r.width, h: r.height};
})()"""

SET_FIELD_JS = r"""(() => {
    // Type-aware field write: checkbox/radio toggle, <select> option by
    // value or label, contenteditable text, plain value otherwise. All
    // paths fire input + change + blur so frameworks and validators run.
    const key = %s, value = %s;
    const el = /^e\d+$/.test(key)
        ? (window.__browserr_refs || {})[key]
        : document.querySelector(key);
    if (!el || !el.isConnected) return {ok: false, error: 'not found: ' + key};
    if (el.focus) { try { el.focus(); } catch (_) {} }
    const tag = el.tagName.toLowerCase();
    const type = (el.type || '').toLowerCase();
    let how = 'value';
    if (type === 'checkbox' || type === 'radio') {
        const want = value === true || value === 1
            || ['true', '1', 'checked', 'yes', 'on'].includes(String(value).toLowerCase());
        if (!!el.checked !== want) {
            // Native click fires input and change already.
            el.click();
            el.dispatchEvent(new Event('blur', {bubbles: true}));
            return {ok: true, how: 'checked', value: !!el.checked};
        }
        how = 'checked';
    } else if (tag === 'select') {
        const opts = [...el.options];
        const v = String(value).trim();
        const hit = opts.find(o => o.value === v)
            || opts.find(o => (o.text || '').trim().toLowerCase() === v.toLowerCase());
        if (!hit) return {ok: false, error: 'no option: ' + v,
                          options: opts.map(o => o.value).slice(0, 60)};
        el.value = hit.value;
        how = 'option';
    } else if (el.isContentEditable) {
        el.textContent = String(value);
        how = 'richtext';
    } else {
        el.value = String(value);
    }
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    el.dispatchEvent(new Event('blur', {bubbles: true}));
    return {ok: true, how: how,
            value: (type === 'checkbox' || type === 'radio') ? !!el.checked : el.value};
})()"""
FILL_FORM_JS = r"""(() => {
    // Batch field write: ONE round trip for a whole form. `plan` maps a
    // stable ref ('e7') or a CSS selector to the value to write; the
    // write is type-aware, same rules as set_field.
    const plan = %s;
    const refs = window.__browserr_refs || {};
    const resolve = (key) => /^e\d+$/.test(key)
        ? refs[key] : document.querySelector(key);
    const apply = (el, value) => {
        if (el.focus) { try { el.focus(); } catch (_) {} }
        const tag = el.tagName.toLowerCase();
        const type = (el.type || '').toLowerCase();
        let how = 'value';
        if (type === 'checkbox' || type === 'radio') {
            const want = value === true || value === 1
                || ['true', '1', 'checked', 'yes', 'on'].includes(String(value).toLowerCase());
            if (!!el.checked !== want) {
                // Native click fires input and change already.
                el.click();
                el.dispatchEvent(new Event('blur', {bubbles: true}));
                return {ok: true, how: 'checked', value: !!el.checked};
            }
            how = 'checked';
        } else if (tag === 'select') {
            const opts = [...el.options];
            const v = String(value).trim();
            const hit = opts.find(o => o.value === v)
                || opts.find(o => (o.text || '').trim().toLowerCase() === v.toLowerCase());
            if (!hit) return {ok: false, error: 'no option: ' + v};
            el.value = hit.value;
            how = 'option';
        } else if (el.isContentEditable) {
            el.textContent = String(value);
            how = 'richtext';
        } else {
            el.value = String(value);
        }
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
        el.dispatchEvent(new Event('blur', {bubbles: true}));
        return {ok: true, how: how,
                value: (type === 'checkbox' || type === 'radio') ? !!el.checked : el.value};
    };
    const out = {ok: true, fields: {}};
    for (const key of Object.keys(plan)) {
        const el = resolve(key);
        if (!el || !el.isConnected) {
            out.ok = false;
            out.fields[key] = {ok: false, error: 'not found: ' + key};
            continue;
        }
        const r = apply(el, plan[key]);
        if (!r.ok) out.ok = false;
        out.fields[key] = r;
    }
    return out;
})()"""

SUBMIT_FORM_JS = r"""(() => {
    // Submit a form by ref/selector (a <form>, a field or a submit
    // button), else the first form on the page. requestSubmit when
    // available; otherwise a submit event and, if not prevented,
    // form.submit(). Never cancels a main-frame navigation.
    const key = %s;
    const refs = window.__browserr_refs || {};
    const pick = (k) => /^e\d+$/.test(k)
        ? refs[k] : document.querySelector(k);
    let el = key ? pick(key) : null;
    if (key && (!el || !el.isConnected)) return {ok: false, error: 'not found: ' + key};
    let form = null;
    if (el && el.tagName === 'FORM') form = el;
    else if (el) form = el.form || (el.closest ? el.closest('form') : null);
    if (!form && document.forms && document.forms.length) form = document.forms[0];
    if (!form) return {ok: false, error: 'no form on the page'};
    const invalid_hint = [];
    for (const f of form.querySelectorAll('input,textarea,select')) {
        if (f.disabled) continue;
        const type = (f.type || '').toLowerCase();
        const val = String(f.value == null ? '' : f.value).trim();
        if (f.hasAttribute('required')
            && ((type === 'checkbox' || type === 'radio') ? !f.checked : val === '')) {
            invalid_hint.push(f.name || f.id || f.tagName.toLowerCase());
        }
    }
    const submitter = (el && (el.tagName === 'BUTTON' || el.tagName === 'INPUT')) ? el : null;
    // Mark that the submit event was seen, so the caller can wait for
    // the dispatch to drain (the click path is asynchronous).
    window.__browserr_submit_seen = 0;
    form.addEventListener('submit', function () {
        window.__browserr_submit_seen = 1;
    }, true);
    // Dispatch the submit event ourselves, then submit natively if the
    // page did not prevent it. servo 0.8's requestSubmit (and a click on
    // a submit button) does NOT reach the page's submit listeners, which
    // silently skips client-side validation; this path is deterministic.
    const ev = new Event('submit', {bubbles: true, cancelable: true});
    const notPrevented = form.dispatchEvent(ev);
    let how = 'submit-event';
    if (notPrevented) {
        if (typeof form.submit === 'function') {
            form.submit();
            how = 'submit-event+native';
        } else if (typeof form.requestSubmit === 'function') {
            form.requestSubmit(submitter || undefined);
            how = 'submit-event+requestSubmit';
        }
    }
    return {ok: true, how: how, action: form.action || '',
            method: (form.method || 'get').toLowerCase(),
            prevented: !notPrevented, invalid_hint: invalid_hint};
})()"""

OUTCOME_JS = r"""(() => {
    // What the page says after an action: url/title, alert and
    // aria-live texts (validation errors, toasts) and fields that
    // violate their required/pattern constraints.
    const out = {url: location.href, title: document.title,
                 alerts: [], invalid_fields: []};
    const seen = new Set();
    for (const el of document.querySelectorAll(
            '[role=alert],[aria-live],[role=status],.error,.errors,.alert,[data-error]')) {
        const t = ((el.textContent || '') + ' ' + (el.getAttribute('data-error') || ''))
            .replace(/\s+/g, ' ').trim();
        if (!t || seen.has(t)) continue;
        seen.add(t);
        out.alerts.push(t.slice(0, 200));
        if (out.alerts.length >= 12) break;
    }
    for (const el of document.querySelectorAll('input,textarea,select')) {
        if (el.disabled || el.readOnly) continue;
        const reasons = [];
        const type = (el.type || '').toLowerCase();
        const val = String(el.value == null ? '' : el.value);
        if (el.hasAttribute('required')
            && ((type === 'checkbox' || type === 'radio') ? !el.checked : val.trim() === '')) {
            reasons.push('required');
        }
        const pat = el.getAttribute('pattern');
        if (pat && val) {
            try {
                if (!new RegExp('^(?:' + pat + ')$').test(val)) reasons.push('pattern');
            } catch (_) {}
        }
        if (reasons.length) {
            out.invalid_fields.push({name: el.name || el.id || '', reasons: reasons});
        }
    }
    return out;
})()"""
HANDOFF_JS = r"""(() => {
    // Things a human must do: CAPTCHAs, one-time codes, passwords with
    // no stored credential, payment fields, and cookie/consent walls.
    // Returns {handoff: bool, reasons: [{kind, detail}], paused_hint}.
    const reasons = [];
    const host = location.hostname;
    const iframe = (sel) => {
        for (const f of document.querySelectorAll('iframe')) {
            if (f.src && f.src.includes(sel)) return f.src;
        }
        for (const s of document.scripts) {
            if (s.src && s.src.includes(sel)) return s.src;
        }
        return null;
    };
    const captcha = iframe('recaptcha') || iframe('hcaptcha')
        || iframe('turnstile') || iframe('grecaptcha');
    if (captcha) reasons.push({kind: 'captcha', detail: captcha.slice(0, 150)});
    for (const i of document.querySelectorAll('input')) {
        const t = (i.type || '').toLowerCase();
        const ac = (i.getAttribute('autocomplete') || '').toLowerCase();
        if (!i.value && (ac.includes('one-time-code') || ac.includes('otp')
                || (t === 'text' && /^\d{4,8}$/.test(i.getAttribute('pattern') || '')
                    && (i.name || i.id || '').toLowerCase().match(/otp|code|token/)))) {
            reasons.push({kind: 'otp',
                          detail: (i.name || i.id || 'input').slice(0, 80)});
        }
        if (!i.value && t === 'password'
                && (ac.includes('current-password') || !ac)) {
            reasons.push({kind: 'password',
                          detail: (i.name || i.id || 'input').slice(0, 80)});
        }
        if (!i.value && (ac.includes('cc-number') || ac.includes('card-number')
                || ac.includes('cc-csc') || ac.includes('card-cvc'))) {
            reasons.push({kind: 'payment',
                          detail: (i.name || i.id || 'input').slice(0, 80)});
        }
    }
    const consent = document.querySelector(
        '[role=dialog] button, [class*=consent] button, [id*=consent] button');
    if (consent && document.querySelector('[class*=cookie], [id*=cookie]')) {
        reasons.push({kind: 'consent',
                      detail: (consent.textContent || '').trim().slice(0, 80)});
    }
    return {handoff: reasons.length > 0, reasons: reasons,
            paused_hint: reasons.length
                ? 'human needed for: ' + reasons.map(r => r.kind).join(', ')
                : ''};
})()"""
