/**
 * JarvisGraph — Estate-Graph als Vollbild-Canvas (Mock, Brief F11).
 *
 * Die SVG ist die vom A4-Mockup statisch gerenderte Vorschau des
 * `#pa-graph-mock`-Datensatzes (graphMock.ts) — verbatime Portierung
 * (Knoten, Kanten, Cluster-Nebel, Fokus-Zustand Hermes-Infra, Label-
 * Staffelung, Sternenstaub). Einzige Anpassung: Gradient/Filter-IDs tragen
 * den Prefix `jv-` (Kollisionsschutz im SPA-DOM). Kein Graph-Endpoint —
 * der kommt mit S2.7 und rendert dann aus derselben Datenstruktur.
 * Der Szenen-Toggle und die LADEN/LEER-Overlays des Mockups sind Mockup-
 * Chrome und werden bewusst NICHT übernommen (Brief).
 */
export function JarvisGraph() {
  return (
    <svg
      className="jv-brain"
      viewBox="0 0 1280 820"
      preserveAspectRatio="xMidYMid slice"
      aria-label="Estate-Graph (Vorschau, Mock-Daten)"
      role="img"
    >
      <defs>
        <filter id="jv-blur" x="-60%" y="-60%" width="220%" height="220%">
          <feGaussianBlur stdDeviation="5" />
        </filter>
        <radialGradient id="jv-fog" cx="50%" cy="45%" r="60%">
          <stop offset="0%" stopColor="#38d8ff" stopOpacity=".06" />
          <stop offset="100%" stopColor="#000" stopOpacity="0" />
        </radialGradient>
        <radialGradient id="jv-orb-canon" cx="38%" cy="30%" r="78%">
          <stop offset="0%" stopColor="#bff3ff" />
          <stop offset="42%" stopColor="#38d8ff" />
          <stop offset="100%" stopColor="#1a647a" />
        </radialGradient>
        <radialGradient id="jv-neb-canon">
          <stop offset="0%" stopColor="#38d8ff" stopOpacity=".13" />
          <stop offset="55%" stopColor="#38d8ff" stopOpacity=".05" />
          <stop offset="100%" stopColor="#38d8ff" stopOpacity="0" />
        </radialGradient>
        <radialGradient id="jv-orb-projekte" cx="38%" cy="30%" r="78%">
          <stop offset="0%" stopColor="#c1f4de" />
          <stop offset="42%" stopColor="#3ddc97" />
          <stop offset="100%" stopColor="#1d664c" />
        </radialGradient>
        <radialGradient id="jv-neb-projekte">
          <stop offset="0%" stopColor="#3ddc97" stopOpacity=".13" />
          <stop offset="55%" stopColor="#3ddc97" stopOpacity=".05" />
          <stop offset="100%" stopColor="#3ddc97" stopOpacity="0" />
        </radialGradient>
        <radialGradient id="jv-orb-agenten" cx="38%" cy="30%" r="78%">
          <stop offset="0%" stopColor="#ffe7c4" />
          <stop offset="42%" stopColor="#ffb347" />
          <stop offset="100%" stopColor="#745428" />
        </radialGradient>
        <radialGradient id="jv-neb-agenten">
          <stop offset="0%" stopColor="#ffb347" stopOpacity=".13" />
          <stop offset="55%" stopColor="#ffb347" stopOpacity=".05" />
          <stop offset="100%" stopColor="#ffb347" stopOpacity="0" />
        </radialGradient>
        <radialGradient id="jv-orb-skills" cx="38%" cy="30%" r="78%">
          <stop offset="0%" stopColor="#cbdaff" />
          <stop offset="42%" stopColor="#5b8cff" />
          <stop offset="100%" stopColor="#2a427a" />
        </radialGradient>
        <radialGradient id="jv-neb-skills">
          <stop offset="0%" stopColor="#5b8cff" stopOpacity=".13" />
          <stop offset="55%" stopColor="#5b8cff" stopOpacity=".05" />
          <stop offset="100%" stopColor="#5b8cff" stopOpacity="0" />
        </radialGradient>
        <radialGradient id="jv-orb-memories" cx="38%" cy="30%" r="78%">
          <stop offset="0%" stopColor="#e8daff" />
          <stop offset="42%" stopColor="#b78cff" />
          <stop offset="100%" stopColor="#53427a" />
        </radialGradient>
        <radialGradient id="jv-neb-memories">
          <stop offset="0%" stopColor="#b78cff" stopOpacity=".13" />
          <stop offset="55%" stopColor="#b78cff" stopOpacity=".05" />
          <stop offset="100%" stopColor="#b78cff" stopOpacity="0" />
        </radialGradient>
        <radialGradient id="jv-orb-receipts" cx="38%" cy="30%" r="78%">
          <stop offset="0%" stopColor="#ffd4e8" />
          <stop offset="42%" stopColor="#ff7ab8" />
          <stop offset="100%" stopColor="#743a5a" />
        </radialGradient>
        <radialGradient id="jv-neb-receipts">
          <stop offset="0%" stopColor="#ff7ab8" stopOpacity=".13" />
          <stop offset="55%" stopColor="#ff7ab8" stopOpacity=".05" />
          <stop offset="100%" stopColor="#ff7ab8" stopOpacity="0" />
        </radialGradient>
        <radialGradient id="jv-orb-archiv" cx="38%" cy="30%" r="78%">
          <stop offset="0%" stopColor="#cad1db" />
          <stop offset="42%" stopColor="#5a6f8f" />
          <stop offset="100%" stopColor="#2a3548" />
        </radialGradient>
        <radialGradient id="jv-neb-archiv">
          <stop offset="0%" stopColor="#5a6f8f" stopOpacity=".13" />
          <stop offset="55%" stopColor="#5a6f8f" stopOpacity=".05" />
          <stop offset="100%" stopColor="#5a6f8f" stopOpacity="0" />
        </radialGradient>
      </defs>
      <rect width="1280" height="820" fill="url(#jv-fog)" />
      {/* Sternenstaub (ruhig, Constellation-Tiefe) */}
      <g>
        <circle cx="1216" cy="452" r="1.3" fill="#cfe4ff" opacity="0.12" />
        <circle cx="934" cy="299" r="0.8" fill="#cfe4ff" opacity="0.29" />
        <circle cx="191" cy="241" r="0.6" fill="#cfe4ff" opacity="0.12" />
        <circle cx="237" cy="43" r="1.2" fill="#cfe4ff" opacity="0.14" />
        <circle cx="339" cy="290" r="1.1" fill="#cfe4ff" opacity="0.19" />
        <circle cx="227" cy="676" r="1.3" fill="#cfe4ff" opacity="0.24" />
        <circle cx="779" cy="412" r="0.9" fill="#cfe4ff" opacity="0.14" />
        <circle cx="646" cy="467" r="0.6" fill="#cfe4ff" opacity="0.11" />
        <circle cx="718" cy="52" r="0.8" fill="#cfe4ff" opacity="0.26" />
        <circle cx="700" cy="60" r="0.7" fill="#cfe4ff" opacity="0.10" />
        <circle cx="75" cy="601" r="0.8" fill="#cfe4ff" opacity="0.29" />
        <circle cx="234" cy="350" r="1.0" fill="#cfe4ff" opacity="0.22" />
        <circle cx="308" cy="560" r="0.9" fill="#cfe4ff" opacity="0.27" />
        <circle cx="165" cy="619" r="0.8" fill="#cfe4ff" opacity="0.16" />
        <circle cx="1081" cy="402" r="1.2" fill="#cfe4ff" opacity="0.12" />
        <circle cx="1069" cy="300" r="0.7" fill="#cfe4ff" opacity="0.12" />
        <circle cx="256" cy="377" r="0.9" fill="#cfe4ff" opacity="0.14" />
        <circle cx="652" cy="213" r="0.8" fill="#cfe4ff" opacity="0.20" />
        <circle cx="580" cy="397" r="0.9" fill="#cfe4ff" opacity="0.25" />
        <circle cx="270" cy="316" r="1.0" fill="#cfe4ff" opacity="0.23" />
        <circle cx="56" cy="26" r="0.5" fill="#cfe4ff" opacity="0.26" />
        <circle cx="449" cy="210" r="0.7" fill="#cfe4ff" opacity="0.15" />
        <circle cx="204" cy="24" r="1.0" fill="#cfe4ff" opacity="0.13" />
        <circle cx="859" cy="193" r="0.6" fill="#cfe4ff" opacity="0.10" />
        <circle cx="911" cy="111" r="0.6" fill="#cfe4ff" opacity="0.30" />
        <circle cx="348" cy="232" r="0.6" fill="#cfe4ff" opacity="0.16" />
        <circle cx="399" cy="648" r="0.8" fill="#cfe4ff" opacity="0.29" />
        <circle cx="520" cy="139" r="0.5" fill="#cfe4ff" opacity="0.17" />
        <circle cx="833" cy="290" r="1.1" fill="#cfe4ff" opacity="0.11" />
        <circle cx="204" cy="52" r="0.7" fill="#cfe4ff" opacity="0.08" />
        <circle cx="67" cy="339" r="1.1" fill="#cfe4ff" opacity="0.08" />
        <circle cx="161" cy="206" r="0.9" fill="#cfe4ff" opacity="0.15" />
        <circle cx="1209" cy="45" r="1.0" fill="#cfe4ff" opacity="0.21" />
        <circle cx="747" cy="178" r="0.7" fill="#cfe4ff" opacity="0.08" />
        <circle cx="935" cy="269" r="0.6" fill="#cfe4ff" opacity="0.16" />
        <circle cx="190" cy="164" r="1.3" fill="#cfe4ff" opacity="0.12" />
        <circle cx="923" cy="171" r="0.5" fill="#cfe4ff" opacity="0.24" />
        <circle cx="824" cy="309" r="1.2" fill="#cfe4ff" opacity="0.30" />
        <circle cx="924" cy="282" r="1.3" fill="#cfe4ff" opacity="0.29" />
        <circle cx="164" cy="729" r="0.9" fill="#cfe4ff" opacity="0.22" />
        <circle cx="578" cy="441" r="1.1" fill="#cfe4ff" opacity="0.27" />
        <circle cx="688" cy="154" r="1.0" fill="#cfe4ff" opacity="0.27" />
        <circle cx="755" cy="422" r="0.8" fill="#cfe4ff" opacity="0.08" />
        <circle cx="1112" cy="694" r="1.1" fill="#cfe4ff" opacity="0.14" />
        <circle cx="986" cy="264" r="1.1" fill="#cfe4ff" opacity="0.12" />
        <circle cx="115" cy="598" r="1.1" fill="#cfe4ff" opacity="0.10" />
        <circle cx="1026" cy="605" r="1.0" fill="#cfe4ff" opacity="0.28" />
        <circle cx="255" cy="557" r="0.7" fill="#cfe4ff" opacity="0.22" />
        <circle cx="1219" cy="363" r="0.7" fill="#cfe4ff" opacity="0.20" />
        <circle cx="726" cy="263" r="1.1" fill="#cfe4ff" opacity="0.27" />
        <circle cx="630" cy="319" r="0.6" fill="#cfe4ff" opacity="0.17" />
        <circle cx="907" cy="495" r="1.1" fill="#cfe4ff" opacity="0.20" />
        <circle cx="1221" cy="556" r="0.5" fill="#cfe4ff" opacity="0.21" />
        <circle cx="1102" cy="372" r="0.8" fill="#cfe4ff" opacity="0.09" />
        <circle cx="710" cy="553" r="1.0" fill="#cfe4ff" opacity="0.22" />
      </g>
      {/* Cluster-Auren (weiche Glow-Nebel, Farben = Filter-Panel) */}
      <g>
        <circle cx="681" cy="175" r="138" fill="url(#jv-neb-canon)" />
        <circle cx="473" cy="612" r="240" fill="url(#jv-neb-projekte)" />
        <circle cx="871" cy="267" r="190" fill="url(#jv-neb-agenten)" />
        <circle cx="422" cy="239" r="194" fill="url(#jv-neb-skills)" />
        <circle cx="1013" cy="430" r="177" fill="url(#jv-neb-memories)" />
        <circle cx="818" cy="600" r="192" fill="url(#jv-neb-receipts)" />
        <circle cx="315" cy="423" r="108" fill="url(#jv-neb-archiv)" />
        <circle cx="640" cy="400" r="98" fill="url(#jv-neb-canon)" />
      </g>
      {/* Kanten: ruhige Kurven, Stärke = Endpunkt-Gewicht */}
      <g>
        <path d="M640 400 Q579.9 332.4 500 290" fill="none" stroke="#7fa8dc" strokeOpacity="0.34" strokeWidth="1.2" />
        <path d="M640 400 Q708.5 336.1 795 300" fill="none" stroke="#7fa8dc" strokeOpacity="0.34" strokeWidth="1.2" />
        <path d="M640 400 Q720.8 447.4 780 520" fill="none" stroke="#7fa8dc" strokeOpacity="0.34" strokeWidth="1.2" />
        <path d="M640 400 Q654.0 322.5 640 245" fill="none" stroke="#7fa8dc" strokeOpacity="0.34" strokeWidth="1.2" />
        <path d="M640 400 Q773.9 383.6 905 415" fill="none" stroke="#7fa8dc" strokeOpacity="0.34" strokeWidth="1.2" />
        <path d="M500 290 Q463.4 249.8 415 225" fill="none" stroke="#7fa8dc" strokeOpacity="0.24" strokeWidth="1.0" />
        <path d="M500 290 Q479.6 314.3 452 330" fill="none" stroke="#7fa8dc" strokeOpacity="0.24" strokeWidth="1.0" />
        <path d="M500 290 Q535.6 264.4 560 228" fill="none" stroke="#7fa8dc" strokeOpacity="0.24" strokeWidth="1.0" />
        <path d="M415 225 Q379.9 210.7 352 185" fill="none" stroke="#7fa8dc" strokeOpacity="0.17" strokeWidth="0.8" />
        <path d="M415 225 Q393.2 240.2 378 262" fill="none" stroke="#7fa8dc" strokeOpacity="0.17" strokeWidth="0.8" />
        <path d="M795 300 Q826.8 267.4 868 248" fill="none" stroke="#7fa8dc" strokeOpacity="0.34" strokeWidth="1.2" />
        <path d="M795 300 Q820.1 324.1 852 338" fill="none" stroke="#7fa8dc" strokeOpacity="0.34" strokeWidth="1.2" />
        <path d="M795 300 Q763.1 274.8 742 240" fill="none" stroke="#7fa8dc" strokeOpacity="0.24" strokeWidth="1.0" />
        <path d="M868 248 Q902.2 235.6 930 212" fill="none" stroke="#7fa8dc" strokeOpacity="0.24" strokeWidth="1.0" />
        <path d="M852 338 Q888.3 338.7 922 352" fill="none" stroke="#7fa8dc" strokeOpacity="0.24" strokeWidth="1.0" />
        <path d="M448 575 Q424.1 602.5 392 620" fill="none" stroke="#7fa8dc" strokeOpacity="0.24" strokeWidth="1.0" />
        <path d="M572 592 Q580.3 626.0 600 655" fill="none" stroke="#7fa8dc" strokeOpacity="0.24" strokeWidth="1.0" />
        <path d="M780 520 Q817.2 540.1 845 572" fill="none" stroke="#7fa8dc" strokeOpacity="0.24" strokeWidth="1.0" />
        <path d="M780 520 Q744.7 549.8 722 590" fill="none" stroke="#7fa8dc" strokeOpacity="0.24" strokeWidth="1.0" />
        <path d="M845 572 Q878.6 586.6 905 612" fill="none" stroke="#7fa8dc" strokeOpacity="0.17" strokeWidth="0.8" />
        <path d="M640 245 Q619.7 208.8 588 182" fill="none" stroke="#7fa8dc" strokeOpacity="0.24" strokeWidth="1.0" />
        <path d="M640 245 Q664.1 207.1 700 180" fill="none" stroke="#7fa8dc" strokeOpacity="0.24" strokeWidth="1.0" />
        <path d="M588 182 Q609.7 159.1 622 130" fill="none" stroke="#7fa8dc" strokeOpacity="0.17" strokeWidth="0.8" />
        <path d="M905 415 Q937.9 397.2 975 392" fill="none" stroke="#7fa8dc" strokeOpacity="0.24" strokeWidth="1.0" />
        <path d="M905 415 Q929.3 443.6 962 462" fill="none" stroke="#7fa8dc" strokeOpacity="0.24" strokeWidth="1.0" />
        <path d="M975 392 Q1006.3 374.0 1042 368" fill="none" stroke="#7fa8dc" strokeOpacity="0.17" strokeWidth="0.8" />
        <path d="M962 462 Q992.8 486.1 1030 498" fill="none" stroke="#7fa8dc" strokeOpacity="0.17" strokeWidth="0.8" />
        <path d="M1042 368 Q1065.1 344.2 1095 330" fill="none" stroke="#7fa8dc" strokeOpacity="0.17" strokeWidth="0.8" />
        <path d="M440 480 Q408.5 459.9 372 452" fill="none" stroke="#7fa8dc" strokeOpacity="0.17" strokeWidth="0.8" />
        <path d="M372 452 Q338.6 444.1 310 425" fill="none" stroke="#7fa8dc" strokeOpacity="0.17" strokeWidth="0.8" />
        <path d="M742 240 Q764.6 209.0 775 172" fill="none" stroke="#7fa8dc" strokeOpacity="0.17" strokeWidth="0.8" />
        <path d="M700 180 Q727.4 154.4 762 140" fill="none" stroke="#7fa8dc" strokeOpacity="0.17" strokeWidth="0.8" />
        <path d="M392 620 Q364.7 636.8 345 662" fill="none" stroke="#7fa8dc" strokeOpacity="0.17" strokeWidth="0.8" />
        <path d="M600 655 Q624.0 673.9 640 700" fill="none" stroke="#7fa8dc" strokeOpacity="0.17" strokeWidth="0.8" />
        <path d="M722 590 Q705.1 620.5 700 655" fill="none" stroke="#7fa8dc" strokeOpacity="0.17" strokeWidth="0.8" />
        <path d="M310 425 Q283.0 412.8 262 392" fill="none" stroke="#7fa8dc" strokeOpacity="0.17" strokeWidth="0.8" />
        <path d="M352 185 Q329.0 163.8 300 152" fill="none" stroke="#7fa8dc" strokeOpacity="0.17" strokeWidth="0.8" />
        <path d="M930 212 Q956.1 190.8 988 180" fill="none" stroke="#7fa8dc" strokeOpacity="0.17" strokeWidth="0.8" />
        <path d="M1030 498 Q1050.8 526.0 1080 545" fill="none" stroke="#7fa8dc" strokeOpacity="0.17" strokeWidth="0.8" />
        <path d="M905 612 Q934.9 626.2 958 650" fill="none" stroke="#7fa8dc" strokeOpacity="0.17" strokeWidth="0.8" />
        <path d="M345 662 Q318.1 676.8 298 700" fill="none" stroke="#7fa8dc" strokeOpacity="0.17" strokeWidth="0.8" />
        <path d="M640 400 Q667.0 550.0 640 700" fill="none" stroke="#7fa8dc" strokeOpacity="0.24" strokeWidth="1.0" />
        <path d="M500 290 Q574.0 280.1 640 245" fill="none" stroke="#7fa8dc" strokeOpacity="0.34" strokeWidth="1.2" />
        <path d="M795 300 Q860.4 347.6 905 415" fill="none" stroke="#7fa8dc" strokeOpacity="0.34" strokeWidth="1.2" />
      </g>
      {/* Fokus-Kanten (statisch gerenderter Fokus-Zustand: Hermes-Infra) */}
      <g>
        <path d="M640 400 Q569.2 449.2 520 520" fill="none" stroke="#3ddc97" strokeOpacity=".6" strokeWidth="1.6" />
        <path d="M520 520 Q479.1 541.0 448 575" fill="none" stroke="#3ddc97" strokeOpacity=".6" strokeWidth="1.6" />
        <path d="M520 520 Q552.5 551.3 572 592" fill="none" stroke="#3ddc97" strokeOpacity=".6" strokeWidth="1.6" />
        <path d="M520 520 Q483.6 492.8 440 480" fill="none" stroke="#3ddc97" strokeOpacity=".6" strokeWidth="1.6" />
      </g>
      {/* Knoten: Gewicht → Radius/Helligkeit; Hubs = Orb-Gradient + Glow */}
      <g>
        <g>
          <circle className="jv-breathe" cx="640" cy="400" r="38" fill="#38d8ff" opacity=".14" filter="url(#jv-blur)" />
          <circle cx="640" cy="400" r="23" fill="none" stroke="#38d8ff" strokeOpacity=".45" strokeWidth="1" />
          <circle cx="640" cy="400" r="14.8" fill="url(#jv-orb-canon)" />
        </g>
        <g>
          <circle cx="640" cy="245" r="19" fill="#38d8ff" opacity=".26" filter="url(#jv-blur)" />
          <circle cx="640" cy="245" r="8.3" fill="url(#jv-orb-canon)" />
        </g>
        <g><circle cx="588" cy="182" r="5.9" fill="#38d8ff" opacity="0.73" /></g>
        <g><circle cx="700" cy="180" r="5.4" fill="#38d8ff" opacity="0.7" /></g>
        <g><circle cx="622" cy="130" r="4.7" fill="#38d8ff" opacity="0.67" /></g>
        <g><circle cx="762" cy="140" r="4.2" fill="#38d8ff" opacity="0.64" /></g>
        <g><circle cx="775" cy="172" r="4.7" fill="#38d8ff" opacity="0.67" /></g>
        <g>
          <circle cx="520" cy="520" r="25" fill="#3ddc97" opacity=".26" filter="url(#jv-blur)" />
          <circle cx="520" cy="520" r="10.9" fill="url(#jv-orb-projekte)" />
          <circle cx="520" cy="520" r="18" fill="none" stroke="#3ddc97" strokeOpacity=".85" strokeWidth="1.5" />
          <circle cx="520" cy="520" r="24" fill="none" stroke="#3ddc97" strokeOpacity=".22" strokeWidth="1" />
        </g>
        <g>
          <circle cx="448" cy="575" r="21" fill="#3ddc97" opacity=".26" filter="url(#jv-blur)" />
          <circle cx="448" cy="575" r="9.0" fill="url(#jv-orb-projekte)" />
        </g>
        <g>
          <circle cx="572" cy="592" r="19" fill="#3ddc97" opacity=".26" filter="url(#jv-blur)" />
          <circle cx="572" cy="592" r="8.1" fill="url(#jv-orb-projekte)" />
        </g>
        <g><circle cx="440" cy="480" r="7.3" fill="url(#jv-orb-projekte)" /></g>
        <g><circle cx="392" cy="620" r="5.4" fill="#3ddc97" opacity="0.7" /></g>
        <g><circle cx="600" cy="655" r="5.4" fill="#3ddc97" opacity="0.7" /></g>
        <g><circle cx="345" cy="662" r="4.7" fill="#3ddc97" opacity="0.67" /></g>
        <g><circle cx="640" cy="700" r="4.7" fill="#3ddc97" opacity="0.67" /></g>
        <g><circle cx="298" cy="700" r="3.8" fill="#3ddc97" opacity="0.62" /></g>
        <g>
          <circle cx="795" cy="300" r="25" fill="#ffb347" opacity=".26" filter="url(#jv-blur)" />
          <circle cx="795" cy="300" r="10.9" fill="url(#jv-orb-agenten)" />
        </g>
        <g>
          <circle cx="868" cy="248" r="19" fill="#ffb347" opacity=".26" filter="url(#jv-blur)" />
          <circle cx="868" cy="248" r="8.1" fill="url(#jv-orb-agenten)" />
        </g>
        <g>
          <circle cx="852" cy="338" r="18" fill="#ffb347" opacity=".26" filter="url(#jv-blur)" />
          <circle cx="852" cy="338" r="7.7" fill="url(#jv-orb-agenten)" />
        </g>
        <g><circle cx="742" cy="240" r="6.3" fill="#ffb347" opacity="0.75" /></g>
        <g><circle cx="930" cy="212" r="5.4" fill="#ffb347" opacity="0.7" /></g>
        <g><circle cx="922" cy="352" r="5.4" fill="#ffb347" opacity="0.7" /></g>
        <g><circle cx="988" cy="180" r="4.2" fill="#ffb347" opacity="0.64" /></g>
        <g>
          <circle cx="500" cy="290" r="23" fill="#5b8cff" opacity=".26" filter="url(#jv-blur)" />
          <circle cx="500" cy="290" r="10.0" fill="url(#jv-orb-skills)" />
        </g>
        <g><circle cx="415" cy="225" r="7.3" fill="url(#jv-orb-skills)" /></g>
        <g><circle cx="452" cy="330" r="6.3" fill="#5b8cff" opacity="0.75" /></g>
        <g><circle cx="560" cy="228" r="5.9" fill="#5b8cff" opacity="0.73" /></g>
        <g><circle cx="352" cy="185" r="5.1" fill="#5b8cff" opacity="0.69" /></g>
        <g><circle cx="378" cy="262" r="4.7" fill="#5b8cff" opacity="0.67" /></g>
        <g><circle cx="300" cy="152" r="3.8" fill="#5b8cff" opacity="0.62" /></g>
        <g>
          <circle cx="905" cy="415" r="21" fill="#b78cff" opacity=".26" filter="url(#jv-blur)" />
          <circle cx="905" cy="415" r="9.0" fill="url(#jv-orb-memories)" />
        </g>
        <g><circle cx="975" cy="392" r="6.7" fill="#b78cff" opacity="0.77" /></g>
        <g><circle cx="962" cy="462" r="6.3" fill="#b78cff" opacity="0.75" /></g>
        <g><circle cx="1042" cy="368" r="5.4" fill="#b78cff" opacity="0.7" /></g>
        <g><circle cx="1030" cy="498" r="5.1" fill="#b78cff" opacity="0.69" /></g>
        <g><circle cx="1095" cy="330" r="4.2" fill="#b78cff" opacity="0.64" /></g>
        <g><circle cx="1080" cy="545" r="3.8" fill="#b78cff" opacity="0.62" /></g>
        <g>
          <circle cx="780" cy="520" r="21" fill="#ff7ab8" opacity=".26" filter="url(#jv-blur)" />
          <circle cx="780" cy="520" r="9.0" fill="url(#jv-orb-receipts)" />
        </g>
        <g><circle cx="845" cy="572" r="6.7" fill="#ff7ab8" opacity="0.77" /></g>
        <g><circle cx="722" cy="590" r="5.9" fill="#ff7ab8" opacity="0.73" /></g>
        <g><circle cx="905" cy="612" r="5.1" fill="#ff7ab8" opacity="0.69" /></g>
        <g><circle cx="700" cy="655" r="4.2" fill="#ff7ab8" opacity="0.64" /></g>
        <g><circle cx="958" cy="650" r="3.8" fill="#ff7ab8" opacity="0.62" /></g>
        <g><circle cx="372" cy="452" r="5.4" fill="#5a6f8f" opacity="0.7" /></g>
        <g><circle cx="310" cy="425" r="4.7" fill="#5a6f8f" opacity="0.67" /></g>
        <g><circle cx="262" cy="392" r="3.8" fill="#5a6f8f" opacity="0.62" /></g>
      </g>
      {/* Labels (Positionen aus dem Mockup, kollisionsbereinigt) + Fokus-Hinweis */}
      <text className="maplabel big" x="662" y="394">vision</text>
      <text className="maplabel" x="626" y="249" textAnchor="end">conventions-gates</text>
      <text className="maplabel dim2" x="560" y="172">planspec-taskgraph</text>
      <text className="maplabel dim2" x="712" y="163">infra-topologie</text>
      <text className="maplabel big" x="538" y="516">Hermes-Infra</text>
      <text className="maplabel" x="400" y="596">Health Track</text>
      <text className="maplabel" x="586" y="612">Family Organizer</text>
      <text className="maplabel dim2" x="404" y="470">Diktat</text>
      <text className="maplabel big" x="812" y="294">Jarvis</text>
      <text className="maplabel" x="880" y="240">Codex</text>
      <text className="maplabel" x="866" y="358">Kimi K3</text>
      <text className="maplabel dim2" x="726" y="262">Grok</text>
      <text className="maplabel" x="516" y="280">Skills</text>
      <text className="maplabel dim2" x="418" y="214">merge-deploy</text>
      <text className="maplabel" x="886" y="419" textAnchor="end">Memories</text>
      <text className="maplabel dim2" x="886" y="484">jarvis-roadmap R47</text>
      <text className="maplabel" x="796" y="510">Receipts</text>
      <text className="maplabel focuslbl" x="520" y="556" textAnchor="middle">· FOKUS ·</text>
    </svg>
  );
}
