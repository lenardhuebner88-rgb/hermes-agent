import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import {
  BroadsheetShell,
  BroadsheetFooter,
  EngpassLead,
  ErrorBar,
  ErrorLegend,
  ErrorLegendItem,
  Kicker,
  LeaderRow,
  LedgerRow,
  Masthead,
  SectionRule,
  SupportingStat,
  SupportingStats,
  TwinStat,
  TwinStats,
  Verdict,
} from "./Broadsheet";
import { broadsheet } from "../../lib/broadsheetTokens";

describe("BroadsheetShell", () => {
  it("carries the [data-stats-broadsheet] scope + sb-wrap so the layer stays tab-local", () => {
    const html = renderToStaticMarkup(<BroadsheetShell>x</BroadsheetShell>);
    expect(html).toContain("data-stats-broadsheet");
    expect(html).toContain("sb-wrap");
  });
});

describe("Masthead", () => {
  it("renders the two kicks, a navy accent label, the display figure + unit, and a status delta", () => {
    const html = renderToStaticMarkup(
      <Masthead
        kicker="Hermes · Flotten-Report"
        meta="17. Juni · 7 Tage"
        label="Akzeptanzrate"
        value="91"
        unit="%"
        note="118 abgenommen · 12 verworfen"
        delta="↑ +4 % ggü. VW"
        deltaStatus="ok"
      />,
    );
    expect(html).toContain("Hermes · Flotten-Report");
    expect(html).toContain("17. Juni · 7 Tage");
    // The accent label is navy via sb-accent; the figure rides the display class.
    expect(html).toContain("sb-kick sb-accent");
    expect(html).toContain("Akzeptanzrate");
    expect(html).toContain('class="sb-mast"');
    expect(html).toContain("<small>%</small>");
    expect(html).toContain("118 abgenommen · 12 verworfen");
    expect(html).toContain("sb-d sb-ok");
    expect(html).toContain("↑ +4 % ggü. VW");
  });

  it("omits the footing line when neither note nor delta is given", () => {
    const html = renderToStaticMarkup(<Masthead kicker="K" value="0" />);
    expect(html).not.toContain("sb-mline");
  });
});

describe("SupportingStats", () => {
  it("renders a three-up with an accent figure and its unit", () => {
    const html = renderToStaticMarkup(
      <SupportingStats>
        <SupportingStat value="94" unit="%" label="Autonomie" accent />
        <SupportingStat value="$1,04" label="je Lieferung" />
        <SupportingStat value="~30" label="Nutzerwert" />
      </SupportingStats>,
    );
    expect(html).toContain("sb-threeup");
    expect(html).toContain("sb-n sb-accent");
    expect(html).toContain("<small>%</small>");
    expect(html).toContain("Autonomie");
    expect(html).toContain("je Lieferung");
    expect(html).toContain("Nutzerwert");
  });
});

describe("SectionRule", () => {
  it("renders the title, the hairline, and the right meta", () => {
    const html = renderToStaticMarkup(<SectionRule title="Budget" meta="Reichweite je Abo" />);
    expect(html).toContain("sb-skick");
    expect(html).toContain("<h2>Budget</h2>");
    expect(html).toContain('class="sb-ln"');
    expect(html).toContain("Reichweite je Abo");
  });
});

describe("EngpassLead", () => {
  it("defaults to the crit spine and switches tone classes", () => {
    expect(renderToStaticMarkup(<EngpassLead>a</EngpassLead>)).toContain('class="sb-lead"');
    expect(renderToStaticMarkup(<EngpassLead tone="warn">a</EngpassLead>)).toContain("sb-lead-warn");
    expect(renderToStaticMarkup(<EngpassLead tone="calm">a</EngpassLead>)).toContain("sb-lead-calm");
  });
});

describe("LedgerRow", () => {
  it("maps status to figure ink + meter fill, clamps the meter, and renders the tag + footing", () => {
    const html = renderToStaticMarkup(
      <LedgerRow name="ChatGPT" figure="96 %" status="crit" pct={96} footLeft="5 Std · 41 %" footRight="Reset in 2 Tg" />,
    );
    expect(html).toContain("sb-led-fig sb-crit");
    expect(html).toContain('class="sb-mr"');
    expect(html).toContain("width:96%");
    expect(html).toContain("5 Std · 41 %");
    expect(html).toContain("Reset in 2 Tg");

    const tagged = renderToStaticMarkup(
      <LedgerRow name="Kimi" tag="geschätzt" figure="~13 %" status="ok" pct={13} />,
    );
    expect(tagged).toContain("sb-tagm");
    expect(tagged).toContain("geschätzt");
    expect(tagged).toContain("sb-led-fig sb-ok");
    expect(tagged).toContain('class="sb-me"');

    // Out-of-range pct is clamped to [0,100].
    expect(renderToStaticMarkup(<LedgerRow name="x" figure="" status="warn" pct={140} />)).toContain("width:100%");
    expect(renderToStaticMarkup(<LedgerRow name="x" figure="" status="warn" pct={-5} />)).toContain("width:0%");
  });
});

describe("TwinStats", () => {
  it("renders two big figures with kick labels and units", () => {
    const html = renderToStaticMarkup(
      <TwinStats>
        <TwinStat label="Median · p50" value="4" unit=" min" />
        <TwinStat label="p90" value="17" unit=" min" />
      </TwinStats>,
    );
    expect(html).toContain("sb-twin");
    expect(html).toContain("Median · p50");
    expect(html).toContain('class="sb-tn"');
    expect(html).toContain("<small> min</small>");
  });
});

describe("LeaderRow", () => {
  it("renders rank · name · status score · latency", () => {
    const html = renderToStaticMarkup(
      <LeaderRow rank={1} name="coder" score="92 %" status="ok" latency="6m" />,
    );
    expect(html).toContain('class="sb-rk"');
    expect(html).toContain("coder");
    expect(html).toContain("sb-sc sb-ok");
    expect(html).toContain("92 %");
    expect(html).toContain("6m");
    // amber score for a sub-threshold worker.
    expect(renderToStaticMarkup(<LeaderRow rank={3} name="premium" score="80 %" status="warn" />)).toContain("sb-sc sb-warn");
  });
});

describe("ErrorBar + legend", () => {
  it("stacks segments by width and fill, clamps, and renders legend items with counts", () => {
    const html = renderToStaticMarkup(
      <ErrorBar
        segments={[
          { pct: 47, color: broadsheet.errorSeries[0] },
          { pct: 26, color: broadsheet.errorSeries[1] },
          { pct: 200, color: broadsheet.errorSeries[2] },
        ]}
      />,
    );
    expect(html).toContain("sb-estack");
    expect(html).toContain("width:47%");
    expect(html).toContain(`background:${broadsheet.errorSeries[0]}`);
    expect(html).toContain("width:100%"); // 200 clamped

    const legend = renderToStaticMarkup(
      <ErrorLegend>
        <ErrorLegendItem color={broadsheet.errorSeries[0]} label="Prozess tot" count={9} />
      </ErrorLegend>,
    );
    expect(legend).toContain("sb-leg");
    expect(legend).toContain("Prozess tot");
    expect(legend).toContain("<b>9</b>");
  });
});

describe("Verdict", () => {
  it("defaults to a calm spine and switches tone classes", () => {
    expect(renderToStaticMarkup(<Verdict>ok</Verdict>)).toContain('class="sb-vd"');
    expect(renderToStaticMarkup(<Verdict tone="warn">w</Verdict>)).toContain("sb-vd-warn");
    expect(renderToStaticMarkup(<Verdict tone="crit">c</Verdict>)).toContain("sb-vd-crit");
  });
});

describe("BroadsheetFooter", () => {
  it("renders the colophon left/right", () => {
    const html = renderToStaticMarkup(<BroadsheetFooter left="Hermes" right="/control/statistik" />);
    expect(html).toContain("sb-foot");
    expect(html).toContain("Hermes");
    expect(html).toContain("/control/statistik");
  });
});

describe("Kicker", () => {
  it("tints navy when accent is set", () => {
    expect(renderToStaticMarkup(<Kicker accent>x</Kicker>)).toContain("sb-kick sb-accent");
    expect(renderToStaticMarkup(<Kicker>x</Kicker>)).not.toContain("sb-accent");
  });
});
