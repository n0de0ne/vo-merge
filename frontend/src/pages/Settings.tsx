import { useEffect, useMemo, useState } from "react";
import { api, type FieldSpec, type SettingsSchema } from "../api";
import { runAction, useStored } from "../lib/poll";
import { go, setParam, useRoute } from "../lib/router";
import { Act, Empty } from "../components/ui";

/** Settings, rendered ENTIRELY from `/api/settings/schema`.
 *
 *  The old page hand-wrote a form for about forty of the 111 config keys, so the other seventy —
 *  every autonomy key, every timeout, the notify channel, the whole disk/recycle policy — could
 *  only be changed by editing config.json on the host and restarting. That shortlist silently
 *  decided which parts of the app an operator was allowed to run. Nothing here knows the name of
 *  a single setting: a key described in `settings_meta.py` appears with its help, its type and
 *  its validation, and this file does not change. That is also why `undescribed` is shown as a
 *  warning rather than swallowed — a key with no description is invisible here, which is the
 *  exact failure the schema exists to end. */
export default function Settings({ section }: { section?: string }) {
  const route = useRoute();
  const [schema, setSchema] = useState<SettingsSchema | null>(null);
  const [edits, setEdits] = useState<Record<string, unknown>>({});
  // Emptying a password box means "unchanged" (that is what the placeholder promises), so
  // clearing a stored secret needs to be said explicitly or it can't be said at all — and
  // hand-editing config.json is precisely what this page exists to replace.
  const [clearing, setClearing] = useState<Record<string, boolean>>({});
  const [tests, setTests] = useState<Record<string, { ok: boolean; msg: string }>>({});
  const [saved, setSaved] = useState(false);
  const [adv, setAdv] = useStored("vo.adv", false);
  const [text, setText] = useState(route.query.get("q") ?? "");

  // Loaded once, never polled: a background refresh would overwrite whatever is half-typed.
  const load = async () => setSchema(await api.settingsSchema());
  useEffect(() => { void runAction(load); }, []);

  // Debounced into the URL so a filtered view is shareable, while the input itself stays local —
  // a round trip through the hash on every keystroke drops characters.
  useEffect(() => {
    const t = setTimeout(() => setParam("q", text || null), 300);
    return () => clearTimeout(t);
  }, [text]);

  const specs = useMemo(() => {
    const m = new Map<string, FieldSpec>();
    schema?.sections.forEach(s => s.fields.forEach(f => m.set(f.key, f)));
    return m;
  }, [schema]);

  const valueOf = (spec: FieldSpec) => (spec.key in edits ? edits[spec.key] : baseEdit(spec));
  const isDirty = (spec: FieldSpec) =>
    !!clearing[spec.key] || (spec.key in edits && !same(edits[spec.key], baseEdit(spec)));

  const dirty = useMemo(
    () => [...specs.values()].filter(isDirty),
    [specs, edits, clearing]);
  const invalid = useMemo(
    () => dirty.filter(s => badNumber(s, valueOf(s))),
    [dirty, edits]);

  // A reload with unsaved edits is the one way this form loses work that the sticky save bar
  // can't prevent, so it costs a browser prompt.
  useEffect(() => {
    if (!dirty.length) return;
    const on = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = ""; };
    window.addEventListener("beforeunload", on);
    return () => window.removeEventListener("beforeunload", on);
  }, [dirty.length]);

  useEffect(() => {
    if (!saved) return;
    const t = setTimeout(() => setSaved(false), 5000);
    return () => clearTimeout(t);
  }, [saved]);

  if (!schema) return <div className="panel">loading settings…</div>;

  const sections = schema.sections;
  const active = sections.find(s => s.id === section) ?? sections[0];
  const q = text.trim().toLowerCase();

  function set(key: string, v: unknown) {
    setEdits(e => ({ ...e, [key]: v }));
    setSaved(false);
  }

  function revert() { setEdits({}); setClearing({}); setSaved(false); }

  /** Only CHANGED keys are posted, and a masked secret never enters `edits` unless it was typed —
   *  so an untouched password can't be written back over the real value as a boolean. */
  async function save() {
    const data: Record<string, unknown> = {};
    for (const spec of dirty) {
      const v = valueOf(spec);
      switch (spec.type) {
        case "bool": data[spec.key] = !!v; break;
        case "number": data[spec.key] = Number(String(v).trim()); break;
        case "list_int":
          data[spec.key] = splitList(String(v)).map(x => parseInt(x, 10)).filter(n => !isNaN(n));
          break;
        case "list_str": data[spec.key] = splitList(String(v)); break;
        case "profiles": data[spec.key] = profilesOut(v as ProfileText); break;
        case "password": data[spec.key] = clearing[spec.key] ? "" : String(v); break;
        default: data[spec.key] = String(v);
      }
    }
    const ok = await runAction(async () => {
      await api.saveSettings(data);
      // Refetched rather than assumed: the backend coerces types and clamps the scheduler keys
      // (search_interval_min: 0 becomes 1), and a form that kept showing what you typed would be
      // reporting a value the app is not running on.
      await load();
    });
    if (ok) { setEdits({}); setClearing({}); setSaved(true); }
  }

  /* ------------------------------------------------------------------ one field
     A plain function, NOT a nested component: a component declared during render is a new type
     on every render, so React unmounts and remounts its subtree — which loses the focus and the
     caret of the input you are typing into. */
  const control = (spec: FieldSpec) => {
    const id = "set-" + spec.key;
    const v = valueOf(spec);
    switch (spec.type) {
      case "bool":
        return <input id={id} type="checkbox" className="sw" checked={!!v}
          onChange={e => set(spec.key, e.target.checked)} />;
      case "number":
        return <>
          <input id={id} type="number" value={String(v)} min={spec.min} max={spec.max}
            step={spec.step} style={{ width: 132 }}
            onChange={e => set(spec.key, e.target.value)} />
          {spec.unit && <span className="unit">{spec.unit}</span>}
          {badNumber(spec, v) && <span className="err">needs a number</span>}
        </>;
      case "select":
        return <select id={id} value={String(v)} style={{ width: "auto" }}
          onChange={e => set(spec.key, e.target.value)}>
          {(spec.options ?? []).map(([val, label]) =>
            <option key={val} value={val}>{label}</option>)}
        </select>;
      case "password":
        return secret(spec, id);
      case "profiles":
        return profiles(spec);
      default:
        return <input id={id} type="text" value={String(v)} style={INPUT}
          onChange={e => set(spec.key, e.target.value)} />;
    }
  };

  const secret = (spec: FieldSpec, id: string) => {
    // The GET masks secrets to "is it set?" (qb_pass to "********"), so `spec.value` is never the
    // real one and the box always starts empty.
    const stored = spec.value === true || (typeof spec.value === "string" && spec.value !== "");
    const typed = String(valueOf(spec) ?? "");
    const armed = !!clearing[spec.key];
    return <>
      <input id={id} type="password" autoComplete="new-password" disabled={armed} style={INPUT}
        placeholder={stored ? "•••••• (saved)" : "not set"} value={typed}
        onChange={e => set(spec.key, e.target.value)} />
      {stored && !typed && (armed
        ? <>
            <span className="warn">will be cleared</span>
            <button className="btn sec small"
              onClick={() => setClearing(c => ({ ...c, [spec.key]: false }))}>keep it</button>
          </>
        : <button className="btn sec small" title="Remove the stored value on save"
            onClick={() => { setClearing(c => ({ ...c, [spec.key]: true })); setSaved(false); }}>
            Clear
          </button>)}
    </>;
  };

  /** `lang_profiles` is nested ({movie:{audio:[],subs:[]}, …}) and is edited as one unit, so the
   *  whole object goes into `edits` under its own key and saves in one piece. It is held as TEXT
   *  rather than as the parsed arrays: splitting on every keystroke deletes the comma at the
   *  moment you type it, which makes a second language impossible to enter. */
  const profiles = (spec: FieldSpec) => {
    const obj = (valueOf(spec) ?? {}) as ProfileText;
    const kinds = [...PROFILE_ORDER.filter(k => k in obj),
                   ...Object.keys(obj).filter(k => !PROFILE_ORDER.includes(k))];
    const row = (kind: string, which: "audio" | "subs") => (
      <div key={kind + which} style={PROF_ROW}>
        <span className="muted">{which === "audio"
          ? (KIND_LABEL[kind] ?? kind) + " audio" : "subtitles"}</span>
        <input type="text" value={obj[kind]?.[which] ?? ""} placeholder="fre, eng"
          onChange={e => set(spec.key,
            { ...obj, [kind]: { ...(obj[kind] ?? {}), [which]: e.target.value } })} />
      </div>
    );
    return <div style={{ display: "flex", flexDirection: "column", gap: 4, width: "100%" }}>
      {kinds.flatMap(k => [row(k, "audio"), row(k, "subs")])}
      <span className="muted" style={{ fontSize: 12 }}>
        <b>orig</b> in the anime audio row is the title's own original language, resolved per
        title — it keeps the Japanese VO on a Japanese show without demanding a Japanese track
        from one made in French (Arcane).
      </span>
    </div>;
  };

  const testBtn = (which: string) => {
    const r = tests[which];
    return <>
      <Act cls="btn sec small" busyLabel="testing…" run={() => runAction(async () => {
        const res = await api.test(which);
        setTests(t => ({ ...t, [which]: { ok: !!res.ok, msg: res.ok ? "ok" : (res.error || "failed") } }));
      })}>Test</Act>
      {r && <span className={r.ok ? "ok" : "bad"}>{r.msg}</span>}
      {/* api.test reads the SAVED config, so a URL typed a second ago is not what it dialled. */}
      {!!dirty.length && <span className="muted">tests the saved settings — save first</span>}
    </>;
  };

  const field = (spec: FieldSpec) => (
    <div key={spec.key}
      className={"field" + (isDirty(spec) ? " dirty" : "") + (spec.danger ? " danger" : "")}>
      <label htmlFor={spec.type === "profiles" ? undefined : "set-" + spec.key}>
        {spec.label}
        {spec.advanced && <span className="adv">advanced</span>}
      </label>
      <div className="ctl">{control(spec)}{spec.test && testBtn(spec.test)}</div>
      <div className="help">
        {spec.help}
        {spec.secret && <> The stored value is never sent back to this page, so leaving the box
          blank keeps it.</>}
        {spec.type === "profiles" && <> Comma-separated three-letter codes.</>}
        {defaultNote(spec) && <> <span className="muted">Default: {defaultNote(spec)}.</span></>}
        <> <code>{spec.key}</code></>
      </div>
    </div>
  );

  /* ------------------------------------------------------------------ search vs one section
     Searching spans every section INCLUDING the advanced fields the toggle hides: someone who
     types "mux_timeout" has named the key, and answering "no matches" because of a view
     preference is the dead end this page is meant to end. */
  const hits = q
    ? sections.map(s => ({
        s, fields: s.fields.filter(f => matches(f, s.label, q)),
      })).filter(x => x.fields.length)
    : [];
  const shown = active.fields.filter(f => adv || !f.advanced);
  const hiddenAdv = active.fields.length - shown.length;

  return (
    <>
      <div className="row toolbar">
        <input className="search" type="search" placeholder="Search all settings…" value={text}
          onChange={e => setText(e.target.value)} />
        <label className="muted row tight" style={{ gap: 6 }}>
          <input type="checkbox" checked={adv} onChange={e => setAdv(e.target.checked)} />
          Show advanced
        </label>
        <div className="spacer" />
        <span className="muted">{specs.size} settings</span>
      </div>

      {!!schema.undescribed.length && (
        <div className="panel warnbar-soft">
          <b className="warn">⚠ {schema.undescribed.length} config key
            {schema.undescribed.length === 1 ? " has" : "s have"} no description</b>
          <div className="muted" style={{ margin: "6px 0" }}>
            They are not rendered below, so the only way to change them is by hand-editing
            <code> /config/config.json</code> on the host — the failure this page exists to end.
            Describe them in <code>backend/app/settings_meta.py</code> and they appear here with
            no frontend change.
          </div>
          <div className="mono">{schema.undescribed.join(", ")}</div>
        </div>
      )}
      {!!schema.stale.length && (
        <div className="panel warnbar-soft">
          <b className="warn">⚠ {schema.stale.length} described key
            {schema.stale.length === 1 ? "" : "s"} no longer exist</b>
          <div className="muted" style={{ margin: "6px 0" }}>
            Described in <code>settings_meta.py</code> but absent from the config defaults, so
            anything typed into them is dropped on save.
          </div>
          <div className="mono">{schema.stale.join(", ")}</div>
        </div>
      )}

      <div className="setlayout">
        <nav className="setnav" aria-label="Settings sections">
          {sections.map(s => {
            const n = s.fields.filter(f => isDirty(f)).length;
            return (
              <button key={s.id} className={!q && s.id === active.id ? "active" : ""}
                aria-current={!q && s.id === active.id ? "page" : undefined}
                onClick={() => { setText(""); go("settings/" + s.id); }}>
                <span aria-hidden="true">{s.icon}</span>
                <span style={{ flex: "1 1 auto", minWidth: 0, overflow: "hidden",
                               textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{s.label}</span>
                {/* A change made here stays pending while you read another section, so the nav
                    has to say where it is or Save looks like it does nothing. */}
                {!!n && <span title={`${n} unsaved`} style={{ color: "var(--accent)" }}>●</span>}
              </button>
            );
          })}
        </nav>

        <div>
          {q ? (
            hits.length ? hits.map(({ s, fields }) => (
              <div className="panel" key={s.id}>
                <div className="panel-head">
                  <b className="h2">{s.icon} {s.label}</b>
                  <div className="sub">{fields.length} matching “{text.trim()}”</div>
                </div>
                {fields.map(field)}
              </div>
            )) : (
              <div className="panel">
                <Empty>
                  No setting matches “{text.trim()}”. Search matches the label, the help text and
                  the config key itself.{" "}
                  <button className="btn sec small" onClick={() => setText("")}>Clear search</button>
                </Empty>
              </div>
            )
          ) : (
            <div className="panel">
              <div className="panel-head">
                <b className="h2">{active.icon} {active.label}</b>
                <div className="sub">{active.blurb}</div>
              </div>
              {shown.map(field)}
              {!!hiddenAdv && (
                <div className="sub" style={{ paddingTop: 10 }}>
                  {hiddenAdv} advanced setting{hiddenAdv === 1 ? "" : "s"} hidden — correct
                  defaults that are easy to make worse.{" "}
                  <button className="btn ghost small" onClick={() => setAdv(true)}>Show them</button>
                </div>
              )}
              {active.id === "general" && (
                <Webhooks token={String(valueOf(specs.get("webhook_token") ?? EMPTY_SPEC) ?? "")}
                  interval={String(valueOf(specs.get("search_interval_min") ?? EMPTY_SPEC) ?? 60)} />
              )}
            </div>
          )}
        </div>
      </div>

      {(!!dirty.length || saved) && (
        <div className="savebar">
          {dirty.length
            ? <b>{dirty.length} unsaved change{dirty.length === 1 ? "" : "s"}</b>
            : <b className="ok">Saved ✓</b>}
          {!!dirty.length && (
            <span className="muted">
              in {[...new Set(dirty.map(s => sections.find(x => x.id === s.section)?.label ?? s.section))].join(", ")}
            </span>
          )}
          {!!invalid.length && (
            <span className="bad">{invalid.map(s => s.label).join(", ")} needs a number</span>
          )}
          <div className="spacer" />
          <Act cls="btn" busyLabel="saving…" run={save}
            disabled={!dirty.length || !!invalid.length}>Save</Act>
          <button className="btn sec" disabled={!dirty.length} onClick={revert}>Revert</button>
        </div>
      )}
    </>
  );
}

/* ==================================================================== webhooks */

/** Kept from the old page because it is the only place these URLs exist: the *arrs have to reach
 *  this app by address, so the block shows the one the browser is already using. */
function Webhooks({ token, interval }: { token: string; interval: string }) {
  const origin = typeof window !== "undefined" ? window.location.origin : "http://<vo-merge>";
  const tok = token ? `?token=${encodeURIComponent(token)}` : "";
  return (
    <div style={{ paddingTop: 14 }}>
      <div className="section-title" style={{ marginTop: 0 }}>Instant pickup (webhooks)</div>
      <div className="muted">
        Without these, a newly imported file waits up to one search interval ({interval} min) for
        the sweep. Add a <b>Connect → Webhook</b> in Radarr and Sonarr, method POST, triggered
        <b> On Import</b> and <b> On Upgrade</b>, pointing at:
        <div className="hookurl">{origin}/api/hook/radarr{tok}</div>
        <div className="hookurl">{origin}/api/hook/sonarr{tok}</div>
        Their <b>Test</b> button works. Only the changed file is probed, so a hook costs one
        mkvmerge call rather than a library sweep. The token above is appended live — save it
        before pasting these.
      </div>
    </div>
  );
}

/* ==================================================================== helpers */

type ProfileText = Record<string, { audio?: string; subs?: string }>;

const KIND_LABEL: Record<string, string> = { movie: "Films", series: "TV shows", anime: "Anime" };
const PROFILE_ORDER = ["movie", "series", "anime"];
const INPUT = { flex: "1 1 240px", width: "auto" } as const;
const PROF_ROW = {
  display: "grid", gridTemplateColumns: "110px minmax(0, 1fr)", gap: "8px", alignItems: "center",
} as const;
// Only ever read for `value`, and only when a key the page names is missing from the schema.
const EMPTY_SPEC = { type: "text", value: "" } as unknown as FieldSpec;

const splitList = (s: string) => s.split(",").map(x => x.trim()).filter(Boolean);
const joinList = (v: unknown) => (Array.isArray(v) ? v.join(", ") : String(v ?? ""));

/** The EDITOR form of a stored value. Lists and language profiles are held as text while being
 *  typed — parsing them on each keystroke would swallow the separator as it is entered. */
function baseEdit(spec: FieldSpec): unknown {
  switch (spec.type) {
    case "bool": return !!spec.value;
    case "number": return spec.value == null ? "" : String(spec.value);
    case "list_str": case "list_int": return joinList(spec.value);
    case "password": return "";        // masked server-side; the box starts empty by definition
    case "profiles": {
      const out: ProfileText = {};
      for (const [kind, rows] of Object.entries((spec.value ?? {}) as Record<string, any>))
        out[kind] = { audio: joinList(rows?.audio), subs: joinList(rows?.subs) };
      return out;
    }
    default: return spec.value == null ? "" : String(spec.value);
  }
}

function profilesOut(t: ProfileText) {
  const out: Record<string, { audio: string[]; subs: string[] }> = {};
  for (const [kind, rows] of Object.entries(t ?? {}))
    out[kind] = { audio: splitList(rows?.audio ?? ""), subs: splitList(rows?.subs ?? "") };
  return out;
}

function same(a: unknown, b: unknown) {
  if (a && b && typeof a === "object" && typeof b === "object")
    return JSON.stringify(a) === JSON.stringify(b);
  return a === b;
}

/** `Number("")` is 0, so an emptied box would silently save a zero — and zero is a real value for
 *  several of these keys (a zero interval used to become a one-second full sweep). */
function badNumber(spec: FieldSpec, v: unknown) {
  if (spec.type !== "number") return false;
  const s = String(v ?? "").trim();
  return s === "" || !Number.isFinite(Number(s));
}

function defaultNote(spec: FieldSpec): string {
  if (spec.secret || spec.type === "profiles" || spec.default == null) return "";
  const d = spec.type === "bool" ? (spec.default ? "on" : "off")
    : Array.isArray(spec.default) ? (spec.default.join(", ") || "empty")
    : String(spec.default) || "empty";
  const cur = spec.type === "bool" ? (spec.value ? "on" : "off")
    : Array.isArray(spec.value) ? (spec.value.join(", ") || "empty")
    : String(spec.value ?? "") || "empty";
  return d === cur ? "" : d;
}

function matches(f: FieldSpec, sectionLabel: string, q: string) {
  const hay = `${f.key} ${f.label} ${f.help ?? ""} ${sectionLabel}`.toLowerCase();
  return q.split(/\s+/).filter(Boolean).every(t => hay.includes(t));
}
