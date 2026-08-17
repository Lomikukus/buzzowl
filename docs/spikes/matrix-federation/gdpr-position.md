# GDPR Position Paper — Cross-Instance Client-Card Sharing over Matrix (Buzzowl)

> **This is not legal advice.** It is a research memo, grounded in the regulation text, EDPB/DSK guidance, and reputable secondary sources, written to inform an engineering go/no-go decision. Before shipping the feature, have a German data-protection lawyer or DPO sign off on the final design, and get a proper LIA (legitimate interest assessment) and ROPA entry drafted. Base case assumes both companies are established in Germany/EU and are themselves controllers (not processors) of their own client data.

---

## 1. Lawful basis for A → B disclosure of business contact data

**Position:** Art. 6(1)(f) (legitimate interest) is a *defensible* basis for A disclosing an individual business contact's name, role, and business email/phone to partner B for B's own sales purposes — but only under a documented, per-share balancing test, and it becomes materially weaker (arguably requires consent, Art. 6(1)(a)) the further the disclosure drifts from what the contact would reasonably expect. Confidence: **medium** (fact-specific, no binding case law directly on point for inter-company partner sharing of this kind).

Reasoning:
- Art. 6(1)(f) requires three cumulative elements: (1) a legitimate interest of the controller or a third party, (2) necessity of the processing for that interest, (3) that interest not overridden by the data subject's rights/interests — a case-by-case balancing test, never a blanket policy. BayLDA guidance specifically addresses disclosing an employee's business contact data to another company: during an active business relationship, the employer/counterparty's interest in smooth business dealings typically outweighs the employee's data-protection interest, *because business contact data is low-sensitivity and employees reasonably expect to be reachable for work purposes*. That logic transfers reasonably well to A disclosing contact X's business card–grade data to partner B, provided the disclosure stays inside a comparable business-relationship context.
- Recital 47 makes "reasonable expectations… at the time and in the context of the collection" the crux of the balancing test, and explicitly flags direct marketing as capable of being a legitimate interest — but this is the controller's/third party's interest, not a free pass; it does not override the expectations test.
- Critical German-specific counter-signal: the old BDSG "Listenprivileg" (§28(3) BDSG-alt), which let list/address data be traded for advertising without consent, was **not carried over into GDPR/BDSG-neu**. German commentary is consistently skeptical of pure "Adresshandel" (address trading) under Art. 6(1)(f) — a balancing test "regularly" fails when data is sold/passed on to a third party purely so that third party can build a marketing list, especially when the individual never anticipated cross-company disclosure at all. Buzzowl's design must stay clearly on the "referral partner in an active, disclosed business relationship" side of this line, not the "we sold your business card to a stranger" side.
- Practical dividing line to build into the product: legitimate interest is defensible when (a) B's outreach purpose is closely related to why A originally had the contact (a B2B sales/partnership context the contact would recognize), (b) the disclosure is a discrete, reviewed, per-contact act by a human at A (not bulk/automatic list export), (c) X's company itself is a mutual business counterparty of both A and B in a plausible scenario (not a cold, unrelated market), and (d) the contact retains an easy opt-out/objection route (Art. 21) exercised through A. Consent (Art. 6(1)(a)) should be treated as the required basis instead whenever the sending user cannot articulate a legitimate-interest story that survives that four-part test — e.g., sharing an entire client list en masse to a partner "just in case," or sharing with a partner in an unrelated industry.
- German practice separates two different bodies of law that are frequently conflated: **GDPR governs whether A may disclose the personal data to B at all** (Art. 6 lawful basis for the disclosure); **UWG §7 governs how B may then contact that person** (cold-call/email restrictions, B2B "other market participants" exception in §7(2) No.2 UWG). A disclosure that is lawful under GDPR does not automatically make B's subsequent outreach lawful under UWG, and vice versa — the product should not conflate "we may legally hand this contact to B" with "B may now cold-email this person"; that second question is UWG's, and B needs to independently establish (e.g., prior business relationship, opt-in, or the narrow B2B exception) before contacting X.

Sources:
- [BfDI/DSK Orientierungshilfe Direktwerbung (2018)](https://www.bfdi.bund.de/SharedDocs/Downloads/DE/DSK/Orientierungshilfen/DSK_20181107_Orientierungshilfe_Direktwerbung.pdf)
- [dr-datenschutz.de — Rechtsgrundlage für die Weitergabe von Kontaktdaten im B2B-Bereich](https://www.dr-datenschutz.de/rechtsgrundlage-fuer-die-weitergabe-von-kontaktdaten-im-b2b-bereich/) (BayLDA-based analysis)
- [gdpr-info.eu Recital 47](https://gdpr-info.eu/recitals/no-47/)
- [EDPB Guidelines 1/2024 on Art. 6(1)(f) GDPR](https://www.edpb.europa.eu/system/files/2024-10/edpb_guidelines_202401_legitimateinterest_en.pdf) — three-step LIA methodology (purpose → necessity → balancing)
- [datenschutzticker.de — Adresshandel: kein berechtigtes Interesse](https://www.datenschutzticker.de/2019/03/adresshandel-kein-berechtigtes-interesse/) and [dr-datenschutz.de — Adresshandel: Was erlaubt der Datenschutz?](https://www.dr-datenschutz.de/adresshandel-was-erlaubt-der-datenschutz/) (Listenprivileg abolition)
- [IHK Region Stuttgart — Werbung mittels Telefon, Telefax, E-Mail: was ist wettbewerbsrechtlich erlaubt?](https://www.ihk.de/stuttgart/fuer-unternehmen/recht-und-steuern/wettbewerbsrecht/richtig-werben/was-ist-erlaubt-684868) (UWG §7 vs. GDPR distinction, §7(2) No.2 B2B exception)
- [rdp-law.de — Nutzung von E-Mail-Adressen ohne Einwilligung: was §7 Abs. 3 UWG erlaubt](https://www.rdp-law.de/blog/blog-details/nutzung-von-e-mail-adressen-ohne-einwilligung-was-7-abs-3-uwg-erlaubt-und-was-nicht.html)

---

## 2. Roles: independent controllers, joint controllers, or processor?

**Position:** A and B are almost certainly **independent (separate) controllers**, not joint controllers and not processor/controller. The product should treat every share as a controller-to-controller disclosure and prompt for a lightweight data-sharing acknowledgment at "connect partner" time, not a joint-controller agreement. Confidence: **high** on the controller/processor question, **medium** on exactly what document to prompt for (a design choice, not a strict legal requirement).

Reasoning:
- Art. 26 joint controllership requires *jointly determined* purposes and means. B is not following A's instructions (that would make B a processor under Art. 28), and A and B are not deciding together why/how the data will be processed — each uses the received client card for its **own, separate** sales purpose, retention policy, and access control, after the point of transfer. EDPB Guidelines 07/2020 give near-identical fact patterns as *not* joint controllership: e.g., a group of companies sharing one CRM database is not automatically joint controllership when each entity enters its own data and uses it only for its own purposes with independently decided access/retention; a chain of independent-purpose processing steps likewise yields independent controllers, not joint ones. Buzzowl's design (per-client share toggle by a human at A, review queue at B, no shared purpose-setting) fits the "independent controller, one-off disclosure" pattern, not joint controllership.
- B is not a processor either — a processor processes strictly "on behalf of" and under the instructions of the controller for the controller's purposes; B instead absorbs the card into its own CRM/sales pipeline for its own commercial purpose. So this is a **controller-to-controller data disclosure**, governed by Art. 6 (lawful basis for A's disclosure) plus each side's independent Art. 5/12–22 obligations for what they do with the data thereafter.
- What to prompt for: not the EU joint-controller Art. 26 arrangement (that's for genuinely joint purposes, e.g. a shared marketing campaign) but a **controller-to-controller data-sharing agreement / terms acknowledgment**, ideally accepted once per partner connection, covering: confirmation each party is an independent controller for received data, the categories/purpose of sharing, each side's obligation to have its own lawful basis, security commitments, the retraction/Art. 19 workflow (see §5), and a statement that Buzzowl (if hosted centrally) or the vendor plays no controller role in the shared data itself. This is recommended practice, not a GDPR-mandated document — GDPR does not mandate a specific contract for controller-to-controller sharing the way Art. 28 mandates a DPA for processors — but the EDPB/ICO both recommend documenting the arrangement.

Sources:
- [gdpr-info.eu Art. 26 GDPR](https://gdpr-info.eu/art-26-gdpr/) and [GDPRhub Article 26 GDPR](https://gdprhub.eu/Article_26_GDPR)
- [EDPB Guidelines 07/2020 on the concepts of controller and processor](https://www.edpb.europa.eu/system/files/documents/2023-10/EDPB_guidelines_202007_controllerprocessor_final_en.pdf) — CRM/group-of-companies and processing-chain examples of independent (non-joint) controllers
- [ICO — Data sharing agreements](https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/data-sharing/data-sharing-a-code-of-practice/data-sharing-agreements/) and [Docue — Controller-to-controller agreements: top 5 FAQs](https://docue.com/en-gb/legal-hub/controller-to-controller-agreements) — confirms no mandated clause set for controller-to-controller sharing, but a documented agreement is recommended practice

---

## 3. Transparency duties toward the contact (Art. 14)

**Position:** B has a hard Art. 14 obligation to inform contact X once B receives X's data from A "behind X's back." The product should treat this as a first-class workflow, not an afterthought: a due-by-date checklist item and a provenance record. Confidence: **high** on the legal duty; **medium** on how squarely the disproportionate-effort exception could realistically be invoked.

Reasoning:
- Art. 14 GDPR applies whenever a controller obtains personal data **not from the data subject directly** — exactly the case when B receives X's card from A over Matrix. B (the recipient) becomes an Art. 14 controller with respect to X the moment the card is accepted out of the review queue.
- Deadlines under Art. 14(3): information must be given within a **reasonable period, at the latest within one month** of obtaining the data; but if B intends to *communicate* with X, the information must be given **at the latest at the time of first contact**; if B further discloses X's data onward, at the latest at that disclosure. In practice for a sales tool, "first contact" will usually be the binding deadline, and it will often arrive before the one-month backstop.
- Required content largely mirrors Art. 13 but Art. 14 additionally requires disclosing **the source of the data** (and whether it came from publicly accessible sources) — i.e., B's privacy notice/first outreach to X should be able to say the data came from partner A, consistent with the provenance record below.
- Art. 14(5)(b) — the "impossible or disproportionate effort" exemption — exists but is a **narrow, high-bar exception primarily built for archival/statistical/scientific/large-scale scenarios**; it is not a realistic default excuse for routine B2B contact sharing where B already has X's business email and role and fully intends to reach out. Relying on it as a standing policy would be weak; it might only plausibly apply in an edge case (e.g., a stale/bounced contact B never actually uses). Recommendation: **do not design the product around this exception** — assume the Art. 14 notice is owed.
- Product implications (recommended practice, not literal GDPR text): (a) when a card moves from "received" to "actively used" (added to B's pipeline / first outreach queued), surface a checklist item "you must inform this contact per Art. 14 GDPR — at first contact, and no later than 1 month from receipt"; (b) auto-populate an Art. 14-compliant notice template (identity of B, purpose, legal basis, source = "received from [A] on [date] via Buzzowl partner share," retention, rights, DPO contact if any) that the sales rep can send/paste; (c) keep an immutable **provenance record** ("received from A on <date>, via partner-share, category: business contact") on every card for audit and for fulfilling Art. 14(2)(f)'s source-disclosure requirement.

Sources:
- [gdpr-info.eu Art. 14 GDPR](https://gdpr-info.eu/art-14-gdpr/) — full text, paragraphs 3 and 5(b)
- [gdpr-info.eu Recitals 60–62](https://gdpr-info.eu/recitals/no-61/) — timing and exceptions rationale
- [Legiscope — GDPR Article 14: Information When Data Collected From Third Party](https://www.legiscope.com/blog/gdpr-article-14-third-party.html)

---

## 4. Data minimisation & purpose limitation — default share scope

**Position:** Default-share should be limited to objectively business-purpose fields; anything reflecting subjective judgment, internal deliberation, or third-party personal data about people other than the shared contact should be excluded by default and require a separate, explicit, per-field opt-in. Confidence: **high** (this is standard Art. 5(1)(b)/(c) minimisation reasoning, not a contested area).

Reasoning: Art. 5(1)(b) purpose limitation and 5(1)(c) data minimisation require that only data adequate, relevant, and limited to what's necessary for B's stated purpose (B2B sales outreach to X's company) be shared — not everything A happens to hold on the client.

Recommended default **share scope**:
- **Share by default:** company profile fields that are public/OSINT-sourced (industry, website, public news, publicly stated pain points), and the contact's business-card-grade fields (name, role/title, business email, business phone, LinkedIn URL, company affiliation).
- **Exclude by default (opt-in only, and some should arguably never be shareable):** private/internal sales notes, meeting transcripts and raw meeting audio/text, personal remarks about the contact (communication style, personality read, "difficult," "friendly with competitor," etc.), sensitive inferences or profiling output (e.g., inferred political leaning, health, family situation, anything that could touch Art. 9 special categories even incidentally), internal deal/pricing strategy, and any third-party personal data mentioned incidentally in notes (other named individuals who aren't the shared contact). Sensitive inferences in particular should be flagged and **hard-blocked** from the share toggle, not just defaulted off — they carry disproportionate risk relative to sales value and are exactly the kind of processing likely to fail an Art. 6(1)(f) balancing test.
- Meeting notes/transcripts deserve special caution beyond minimisation: they were captured for A's own knowledge-management purpose, likely contain third-party voices and off-the-cuff remarks never intended for an external company, and sharing them externally is a materially different, riskier processing purpose than sharing a static contact card.

Sources: derived from GDPR Art. 5(1)(b)–(c) principles (gdpr-info.eu) applied to the described data model; no single external source is authoritative here — this is engineering judgment applying the minimisation principle, flagged as **recommended practice**, not a specific regulatory citation.

---

## 5. Data subject rights across instances (erasure/rectification, Art. 19)

**Position:** Art. 19 creates a real, non-optional notification duty on A once A grants an erasure/rectification/restriction request from X and had earlier disclosed X's data to B — the product must support a recipient list per record and a retraction event. What it cannot do is force B to actually delete its copy, and the product should say so plainly rather than imply a guarantee. Confidence: **high**.

Reasoning:
- Art. 19: "The controller shall communicate any rectification or erasure of personal data or restriction of processing carried out in accordance with Article 16, Article 17(1) and Article 18 to each recipient to whom the personal data have been disclosed, unless this proves impossible or involves disproportionate effort." Since Buzzowl's design tracks every share as a discrete, logged event to a specific partner (not an untraceable bulk export), "impossible or disproportionate effort" is **not a credible excuse** here — the recipient is known, so A must notify B. Art. 19 also entitles X, on request, to be told which recipients received the data.
- Product must-haves (this part follows directly from Art. 19, not just recommended practice): (a) a **recipient list per shared record** (which partner instances received this card, when); (b) a **retraction/notification event**: when A processes an erasure/rectification/restriction for X, automatically fire a Matrix event to every partner B that received that card, requesting deletion/update, and log that the notice was sent (satisfies A's Art. 19 duty to *notify*); (c) an **audit log** A can show a regulator or the data subject proving notification occurred.
- What genuinely cannot be guaranteed: Art. 19 obliges A to *notify* B, not to guarantee B's compliance. Once accepted into B's review queue and merged into B's own controllership, **B's copy is outside A's technical and legal control** — A cannot force-delete data on infrastructure it doesn't operate, and Matrix's federated/E2EE model offers no cryptographic erasure guarantee for data another homeserver has already stored (see §9). Honest phrasing for the product/UI and any customer-facing terms: *"When you retract or a data subject's erasure request is processed, we automatically notify every partner who received this record and log that notice for your records. We cannot verify or force deletion on a partner's own system — that is the receiving company's independent obligation as a separate controller."* This honesty is itself good compliance practice (avoiding a misleading claim that could itself create liability).

Sources:
- [gdpr-info.eu Art. 19 GDPR](https://gdpr-info.eu/art-19-gdpr/)
- [gdpr-info.eu Art. 17 GDPR (right to erasure)](https://gdpr-info.eu/art-17-gdpr/) and [Art. 16 (rectification)](https://gdpr-info.eu/art-16-gdpr/) — the underlying rights Art. 19 attaches to

---

## 6. Records of processing (Art. 30), security (Art. 32), DPIA (Art. 35)

**Position:** Art. 30 records are very likely required in practice despite the <250-employee exemption, because the sharing is a **regular, non-occasional** business function, not occasional processing — so build a minimal ROPA entry for the feature as a product/compliance deliverable regardless of company size. Art. 32 (security) is well served by E2EE + self-hosting, but that is only one control among several the controller still owns. A full DPIA is very likely **not required** at the scale described. Confidence: **medium-high**.

Reasoning:
- Art. 30(5) exempts organisations with fewer than 250 employees from the records duty **unless** the processing is (i) likely to result in a risk to data subjects' rights and freedoms, (ii) not occasional, or (iii) involves special-category/Art. 10 data. Cross-instance client-card sharing as a *standing product feature* is by design recurring/systematic (not occasional), which alone is enough to defeat the small-business exemption for this specific processing activity — regardless of either company's headcount. Recommendation: ship a short, templated ROPA entry ("partner client-card sharing") that the product can help generate (purpose, categories, recipients = named partners, retention, security measures) so customers aren't left to figure this out themselves.
- Art. 32 requires "appropriate technical and organisational measures" including, explicitly, encryption (Art. 32(1)(a)) and confidentiality/integrity/availability/resilience. End-to-end encryption of the Matrix room content plus self-hosting is a strong, citable technical measure and a genuinely good argument in a DPIA or Art. 30 entry — but it does not by itself satisfy Art. 32: access control (device verification, key management, who can be a "bot account" admin), backup/retention limits, and breach-detection/notification (Art. 33/34) still have to be designed and owned by each controller.
- DPIA (Art. 35(1)) is triggered by processing "likely to result in a high risk," with Art. 35(3) listing presumptive triggers: large-scale special-category processing, systematic large-scale monitoring of public areas, or systematic/extensive automated evaluation with legal/similarly significant effects. B2B business-contact-card sharing at the scale described (ordinary contact fields, human-reviewed per-share, no profiling with legal effect, no Art. 9 data by design per §4) does not match any Art. 35(3) trigger and is not the kind of processing WP29's nine-criteria DPIA test (large scale, systematic monitoring, sensitive data, vulnerable subjects, innovative technology, etc.) would flag as high-risk on its own. **Position: no DPIA required for the base feature as designed**, provided the minimisation defaults in §4 hold (no sensitive inferences, no bulk automated sharing) — but re-evaluate if a future version adds automated/bulk sharing, scoring, or profiling of contacts.

Sources:
- [gdpr-info.eu Art. 30 GDPR](https://gdpr-info.eu/art-30-gdpr/) (paragraph 5 exemption and its three carve-outs)
- [gdpr-info.eu Art. 32 GDPR](https://gdpr-info.eu/art-32-gdpr/)
- [gdpr-info.eu Art. 35 GDPR](https://gdpr-info.eu/art-35-gdpr/) (high-risk triggers, DPIA content requirements)

---

## 7. Cross-border transfers (Chapter V)

**Position:** Restrict v1 to partners established in the EU/EEA (plus, if desired, formally adequate third countries); treat any non-EEA/non-adequate partner as a Chapter V transfer requiring SCCs and a documented transfer-risk assessment before enabling the connection. Confidence: **high** on the legal requirement, **high** on the v1 product recommendation.

Reasoning:
- Chapter V (Arts. 44–49) applies whenever personal data is disclosed to a controller "in a third country" without an adequacy decision — SCCs (the 2021 modernised set) or another Art. 46 safeguard, plus (post-Schrems II) a transfer impact assessment, are then required. Getting this right (choosing the correct SCC module — here, controller-to-controller — executing it per partner pair, assessing the destination country's surveillance-access regime) is meaningfully more legal overhead than the base EU-EU case, and it doesn't map cleanly onto a lightweight "connect partner" UX flow.
- The EU maintains a growing but still short adequacy list (as of 2026: UK, Switzerland, Japan, South Korea, Canada (commercial), Israel, New Zealand, Argentina, Brazil (Jan 2026), Andorra, Faroe Islands, Guernsey, Isle of Man, Jersey, and the US under the EU-US Data Privacy Framework, among others) — adequacy decisions are also revocable/reviewable (the UK's carries a sunset review, the US DPF has faced ongoing legal challenge), so "adequate today" is not a permanently safe assumption to hard-code.
- **v1 recommendation:** gate the partner-connect flow to EU/EEA-established instances only (verified at minimum by self-declaration + org address at signup). If/when non-EEA partners are supported, require an explicit acknowledgement step referencing the destination country, auto-attach the correct SCC module text to the in-product partner agreement (§2), and re-review adequacy-list status periodically since it changes.

Sources:
- [European Commission — Standard Contractual Clauses (SCC)](https://commission.europa.eu/law/law-topic/data-protection/international-dimension-data-protection/standard-contractual-clauses-scc_en)
- [Gibson Dunn — European Commission Adopts New SCCs (2021)](https://www.gibsondunn.com/european-commission-adopts-new-standard-contractual-clauses-for-international-data-transfers-and-data-processing-agreements/)
- [recordinglaw.com — EU Adequacy Decisions: Full Country List and 2026 Updates](https://www.recordinglaw.com/world-laws/world-data-privacy-laws/eu-adequacy-decisions/)

---

## 8. Sender/rep identity in share events

**Position:** Minor but not zero — the sales rep's own identity (name, Matrix user ID) embedded in every share/audit event is itself personal data of an employee, processed for A's/B's own legitimate operational/accountability interest (Art. 6(1)(f), audit trail); keep it, but apply normal retention limits and don't expose more of the rep's identity externally than the room membership already requires. Confidence: **medium** (straightforward, low-risk employee-data processing, not deeply researched beyond general principle).

Reasoning: Audit/accountability logging of who shared what and when is itself a well-established legitimate interest (security, dispute resolution, Art. 5(2) accountability), so no special basis beyond Art. 6(1)(f)/company policy is needed — just don't let sender identity linger indefinitely or leak into fields visible to the partner beyond what's operationally needed (e.g., no need to expose a rep's personal mobile number in event metadata).

---

## 9. Matrix as the transport: homeserver operator's role, metadata, and hosting choice

**Position:** Self-host a dedicated homeserver per customer (or a Buzzowl-operated, EU-based homeserver under a proper Art. 28 processor DPA) — do **not** default to the public matrix.org homeserver or any third-party-operated federation partner for this feature. The homeserver operator is functionally a processor (or at minimum a critical sub-processor) for metadata and infrastructure even though E2EE limits its access to room *content*. Confidence: **medium-high** (well-supported technically; the controller/processor characterisation of a homeserver operator isn't the subject of binding regulatory guidance, so this is reasoned analysis, not a cited legal conclusion).

Reasoning:
- E2E encryption (Olm/Megolm) protects message *content*, not the surrounding metadata: room membership, sender/device IDs, timestamps, and (depending on deployment) IP addresses remain visible to whichever homeserver(s) host the room, and this metadata is itself personal data under GDPR (it reveals who communicates with whom and when). A homeserver operator storing and relaying this metadata on behalf of a Buzzowl customer is processing personal data "on behalf of" that customer for infrastructure purposes — the classic processor fact pattern — even where it cannot read the encrypted payload.
- Federation is the specific risk to design against: in native Matrix, room events (including metadata, and historically even after "deletion," redacted-but-still-signed events in the room DAG) replicate to every homeserver participating in the room. If either company's homeserver federates outward to unknown third parties, or if a public homeserver like matrix.org is used, personal data can end up replicated onto infrastructure with no operator agreement, no clear jurisdiction, and no reliable deletion guarantee across the federation — a serious Chapter V/Art. 28 problem, independent of E2EE.
- Recommendation (**recommended practice**, not a literal GDPR provision, but a defensible risk-reduction requirement given the above): (a) each customer either self-hosts its own homeserver or Buzzowl operates a dedicated homeserver per customer/tenant under a standard Art. 28 DPA; (b) federation is restricted to the explicit set of invited partner homeservers only (deny-by-default federation, not open federation to arbitrary servers), consistent with what commercial Matrix deployments already do for exactly this reason; (c) do not rely on public/free third-party homeservers (matrix.org or unknown community servers) for any tenant carrying client-card data, since there is no enterprise DPA, no SLA on deletion, and unclear data-residency; (d) treat retraction (§5) as best-effort at the federation layer for the same reason B's own copy can't be force-deleted — signed events in a federated room are hard to fully erase once replicated.

Sources:
- [Wire — Why Matrix Fails EU Data Privacy Standards](https://wire.com/en/blog/matrix-not-safe-eu-data-privacy) — **competitor content, read with skepticism on framing/conclusions, but the underlying technical claims about metadata exposure and federation replication are consistent with independent sources below**
- [anarc.at — Matrix notes (2022)](https://anarc.at/blog/2022-06-17-matrix-notes/) — independent technical write-up covering federation replication and erasure limitations
- [UBports Forum — Matrix, how it works and issues about privacy and GDPR compliance](https://forums.ubports.com/topic/3039/matrix-how-it-works-and-issues-about-privacy-and-gdpr-compliance) — community discussion of the same GDPR/federation tension
- [Matrix.org Foundation Privacy Notice](https://matrix.org/legal/privacy-notice/) — for context on what the public homeserver operator itself discloses about its own data handling

---

## 10. Bottom line + go/no-go checklist

**Bottom-line paragraph for the go/no-go report:**

> Cross-instance client-card sharing over Matrix is GDPR-defensible as designed — controller-to-controller disclosure of business-card-grade contact data under Art. 6(1)(f), not consent, not joint controllership, not processor — *provided* the product enforces: a genuine per-share human decision (no bulk/automatic export), a minimised default share scope excluding notes/transcripts/sensitive inferences, an enforced Art. 14 notice workflow on the receiving side, an Art. 19 retraction/notification pipeline tied to erasure and rectification, an EU/EEA-only v1 partner scope, and a self-hosted or dedicated (non-public) Matrix homeserver per tenant under a processor agreement. None of these are exotic asks — they are the difference between "defensible individual disclosures in an ongoing business relationship" and "address trading," which is the line German regulators actually draw. The weakest links are (1) B's real-world discipline in sending the Art. 14 notice, which the product must actively prompt rather than assume, and (2) the inherent inability to guarantee deletion on federation/partner infrastructure once shared — both should be surfaced honestly in the product rather than glossed over. Recommendation: **go**, conditioned on the checklist below being in v1, not deferred to "later."

**Product requirements checklist (max 12) to keep the feature defensible:**

1. Per-client, per-partner **share toggle is a discrete human action** by the sending user — no bulk/auto-share of the whole client base. *(supports Art. 6(1)(f) balancing, §1)*
2. **Default share scope excludes** private notes, meeting transcripts, personal remarks, and sensitive inferences; these require separate explicit opt-in (some — sensitive inferences — should be hard-blocked). *(§4)*
3. **Receiving-side review queue**: shared cards are badged, read-only, never auto-merged into B's own records without a human accept step. *(supports independent-controller framing, §2)*
4. **In-product partner agreement/terms** accepted once at "connect partner," documenting each side as an independent controller, purpose, security commitments, and the retraction workflow. *(§2)*
5. **Art. 14 checklist/nudge** on the receiving side: "inform this contact at first outreach, no later than 1 month," with a pre-filled compliant notice template. *(§3)*
6. **Provenance record** on every card: received-from, date, share channel — supports Art. 14(2)(f) source disclosure and audit. *(§3, §5)*
7. **Recipient list per shared record** + automated **retraction event** fired to all recipients on erasure/rectification/restriction, with an audit log of notice sent. *(§5, Art. 19)*
8. Honest in-product/legal copy: retraction notifies partners but **cannot force or verify deletion** on their systems. *(§5)*
9. **v1 restricted to EU/EEA-established partner instances**; non-EEA partners blocked or gated behind an SCC-backed flow with explicit acknowledgement. *(§7)*
10. **Dedicated/self-hosted homeserver per tenant**, federation allow-listed to explicitly invited partner servers only — no public/shared third-party homeserver for tenant data. *(§9)*
11. Minimal **ROPA entry template** shipped/generated for the "partner client-card sharing" processing activity, regardless of customer headcount. *(§6)*
12. **Objection/opt-out path** for the underlying contact, exercised through the sending company A (Art. 21), reachable without needing to know the feature exists.

---

*Prepared as an engineering-facing research memo, 2026-08-17. Sources are cited inline per section; where no external source is cited (product-recommendation items), that is flagged as engineering/product judgment rather than a specific legal requirement.*
