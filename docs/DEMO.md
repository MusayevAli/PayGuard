# PayGuard / SiliconSign live demo

**Core claim:** agents can make mistakes; payment authorization remains a separate boundary. Show that boundary refusing a bad request, then show useful work still finishing.

This repo's current ledger entity is a simulated European manufacturer. The city accounts-payable story is a future application of the same boundary. The demo bank moves simulated funds. Do not describe the application as a live city deployment or real banking integration.

## Live invoice workflow

Open **Demo** at `http://127.0.0.1:8000/#demo`. Choose **Load example invoice** to draft fresh text from an existing purchase order and unconsumed goods receipts. This only reads supporting records; it does not invent a purchase order or receipt. Edit the text, then choose **Process live**.

The new request returns immediately. The screen polls its recorded progress while the invoice agent extracts and checks the text, the vendor agent handles any vendor-related issue, and the payment agent proposes an eligible payment. The cards show actual recorded actions and explanations; an agent with no relevant work is shown as having no action. The source text, event timeline, evidence packet and FPGA response remain together on screen.

The indicator stays gray until a verified FPGA response exists. A software rejection can stop an invoice before any FPGA request. Red means a recorded refusal; green requires the verified hardware result. A transport failure is shown as unavailable, not a policy refusal or success. If approval is required, review the evidence packet and use the existing exact-payment authorization control. The approval threshold applies to cumulative vendor spending during the hardware day, so a small invoice can need approval after an earlier large payment.

For your own input, paste the supplier name and VAT ID, a unique invoice number, dates, PO reference, currency, SKU, quantity, unit price, net, VAT and total. The **Add a test invoice** form below is an alternative for structured input; it uses a structured mapper rather than LLM text extraction. The **Dataset** tab exposes the supporting records. A missing PO, duplicate, unmatched supplier or wrong total should be held, not forced through to hardware.

## Control password

This is the local operator credential for submitting invoices, running the desk and recording human decisions. It is separate from the Anthropic API key and the FPGA role keys. Browsing the data requires no password. Open **Settings**, paste the password and choose **Unlock controls**; the server checks it before enabling access.

The launcher copies it to the Mac clipboard. To copy it again without printing it:

```bash
pbcopy < "$HOME/PayGuard-demo-r5/PayGuard-private-ready/private/controller-token"
```

Keep it private. Reloading or closing the tab clears its in-memory access.

## Prepare before presenting

Use the existing hardware build and documented hardware startup procedure. Agents and API calls run on the host computer; fixed payment rules run on the FPGA. USB serial carries payment frames, so FPGA Ethernet is not required. Keep the dashboard's actual signer mode visible. `stub` means a software signer; it does not establish a hardware claim.

Keep the existing hardware database, keys and board session. Each **Run new test** or fresh invoice submission gets its own request identity. Retries of that identity return the same job and cannot submit another payment. Restarted unfinished jobs are marked interrupted rather than automatically replayed. **View last result** only reads earlier evidence. Do not reset the database or FPGA for another rehearsal: hardware caps and replay state remain authoritative.

Provision approved fixture accounts through the trusted setup flow, then close provisioning. Wait for the real hardware hold period to elapse. Check the actual chip status, account slots, transport connection and available cap before presenting. The demo does not shorten holds or change caps. Record a short backup video showing the mode and actual receipts.

The three stage controls require the controller credential. No stage approves itself. Without a physical button, describe the available approval control as **authenticated software approval**, and explain that its trusted path is part of the demonstration's threat model. A mouse click is not independent physical presence. The application must not claim otherwise.

## Three-minute script

| Time | Action | Say |
|---|---|---|
| 0:00–0:25 | Show signer mode and desk | “PayGuard runs an accounts-payable desk: invoice checking, vendor maintenance, and payment scheduling. The payment agent can request a transfer. A separate signer must authorize it.” |
| 0:25–1:00 | **Load example invoice**, edit its text, then **Process live** | “This is new information arriving now. These are the agents' actual checks and explanations. Matching records create a balanced payable; the payment agent requests authorization, and this panel displays the FPGA's verified response.” |
| 1:00–1:40 | Run **Assume the agent is already fooled** | “For this test, we deliberately start after the desk accepts forged business data: an accepted payable points to an account that was never enrolled on the signer. We do not rely on an AI spotting the fraud. The real signer rejects the request, and the simulated bank balance does not move.” |
| 1:40–2:30 | Run **A legitimate exception reaches a human**, inspect packet, authorize through controller flow | “A large legitimate invoice reaches a human with evidence and a specific decision. Review alone does not spend money. The separate authorization completes only this permitted payment.” |
| 2:30–3:00 | Show audit and double-entry lines; invite a judge message | “The interface connects every result to its inputs, decision, signer response and accounting entry. You can submit a different vendor message live. Our claim is that this boundary remains useful when the agent is wrong.” |

If a stage fails, show its actual status and error. Do not turn a transport failure, cap refusal or incomplete fixture into a success badge. If hardware is disconnected, the request fails; there is no automatic software-signer fallback.

## What the three controls actually exercise

`POST /demo/jobs/invoice` accepts new raw or structured invoice input. `POST /demo/jobs/normal`, `/attack`, or `/large` starts an explicitly labeled repeatable fixture. Each returns HTTP 202; `GET /demo/jobs/{run_id}` reads live progress and evidence. Controller authorization protects the POST routes. The older synchronous scenario routes remain compatible. Existing packet and controller authorization routes complete an approval case; this router never invokes a physical or simulated button.

The separate normal and large fixtures add a matching PO, receipt and structured test invoice for an existing, already provisioned EUR vendor. They run ordinary invoice validation, payment policy, selected signer, simulated bank and ledger. Structured invoices do not need a live extraction model. The fresh-text workbench above uses the configured LLM extraction boundary; its example loader only reads existing supporting records. Natural-language explanations follow the application's configured LLM mode. Neither text intake nor these fixtures establish PDF extraction accuracy.

The attack stage explicitly models an agent or desk that has already accepted forged business data. The trusted operator service, its signing keys and the operating system remain trusted; this does not model root compromise. Under the agent-runner lock, it temporarily substitutes an unverified host-side account for a known, provisioned EUR vendor and requests payment against a balanced payable. The original host account is restored in a `finally` block; the fake account is retained as a retired audit artifact. No human approval is invented and the chip enrollment never changes. Using a known vendor sends the wrong account to the FPGA instead of stopping at the host's unknown-vendor check. Its expected protection is account rejection, but the UI displays the response actually received. This is a boundary fault-injection test, not a forensic replay of the Pittsburg incident or proof that a live model was fooled by the exact original email.

The FPGA enforces committed-batch replay prevention, account rules, caps, holds and exact approvals. The desk and bank remain responsible for matching source documents and preventing economic obligations from being paid again under a different batch.

The generated world and scored replay are separate evaluation tools. The live scenarios do not read the answer key or inject “caught” scores. Live business-credit, sanctions and identity checks should only be claimed when a configured integration has actually returned evidence; a configured key or seeded vendor is insufficient.

## Track fit and official requirements

The [official Cal Hacks 13 page](https://cal-hacks-13-0.devpost.com/) names Hyphenate's track **The Hyphenated Enterprise: Autonomous Systems for Finance**. Its public prize listing gives $8,000/$5,000/$3,000 to the top three teams and expedited interviews for the top five.

The [official Hive prize page](https://hive.hackberkeley.org/prizes), backed by its [public prize feed](https://hive.hackberkeley.org/api/live/prizes), publishes four sponsor criteria: credible messy data and evidence; whether the system earns its business roles with limited permissions, an audit record and escalation; a fresh autonomous application or approach; and a memorable catch, recovery or defensible decision.

Hyphenate specifically asks for a video that starts with that memorable moment, a live demonstration that can accept a judge's scenario, a sentence naming the jobs filled, and evidence distinguishing real components from simulations. Open the **video** with the attack refusal and signer receipt, then explain how the desk reached it. The **live** three-minute sequence above establishes normal work before the attack and recovery. The seats sentence is: “PayGuard fills the invoice-checking, vendor-maintenance and payment-scheduling seats; the controller retains exception approval.”

The overall criteria are **Application, Intentionality, Functionality, Creativity, Technical Complexity**. For this project, show the finance workflow, a clear controller decision, a reliable working demonstration, the failure-tolerant design, and the hardware/host boundary. Those are our presentation choices, not additional official criteria.

The official submission page requires a project image, code repository and demonstration, with three minutes to pitch and two minutes for questions. It also asks for a meaningful video for asynchronous sponsor review and work completed during the hackathon. Verify the current submission deadline on that page. Prize outcomes are decided by the judges.
