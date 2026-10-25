# PayGuard / SiliconSign live demo

**Core claim:** agents can make mistakes; payment authorization remains a separate boundary. Show that boundary refusing a bad request, then show useful work still finishing.

This repo's current ledger entity is a simulated European manufacturer. The city accounts-payable story is a future application of the same boundary. The demo bank moves simulated funds. Do not describe the application as a live city deployment or real banking integration.

## Prepare before judges arrive

Use the existing hardware build and documented hardware startup procedure. Agents and API calls run on the host computer; fixed payment rules run on the FPGA. USB serial carries payment frames, so FPGA Ethernet is not required. Keep the dashboard's actual signer mode visible. `stub` means a software signer; it does not establish a hardware claim.

Use a dedicated demo database selected explicitly at startup. The demo API never deletes state or resets money. Each stage is once per database, including after restart. Repeated requests return the existing outcome. For another rehearsal, choose a new database explicitly; preserve the old audit record. Hardware daily spending and replay state are not reset by choosing a new host database.

Provision approved fixture accounts through the trusted setup flow, then close provisioning. Wait for the real hardware hold period to elapse. Check the actual chip status, account slots, transport connection and available cap before presenting. The demo does not shorten holds or change caps. Record a short backup video showing the mode and actual receipts.

The three stage controls require the controller credential. No stage approves itself. Without a physical button, describe the available approval control as **authenticated software approval**, and explain that its trusted path is part of the demonstration's threat model. A mouse click is not independent physical presence. The application must not claim otherwise.

## Three-minute script

| Time | Action | Say |
|---|---|---|
| 0:00–0:25 | Show signer mode and desk | “PayGuard runs an accounts-payable desk: invoice checking, vendor maintenance, and payment scheduling. The payment agent can request a transfer. A separate signer must authorize it.” |
| 0:25–0:55 | Run **A legitimate invoice gets paid** | “Here is the purchase order, receipt and structured invoice. The existing invoice agent checks them, posts a balanced liability, and the payment agent sends a real signing request. This receipt records what the selected signer and simulated bank actually returned.” |
| 0:55–1:40 | Run **Assume the agent is already fooled** | “For this test, we deliberately start after the desk accepts forged business data: an accepted payable points to an account that was never enrolled on the signer. We do not rely on an AI spotting the fraud. The real signer rejects the request, and the simulated bank balance does not move.” |
| 1:40–2:30 | Run **A legitimate exception reaches a human**, inspect packet, authorize through controller flow | “A large legitimate invoice reaches a human with evidence and a specific decision. Review alone does not spend money. The separate authorization completes only this permitted payment.” |
| 2:30–3:00 | Show audit and double-entry lines; invite a judge message | “The interface connects every result to its inputs, decision, signer response and accounting entry. You can submit a different vendor message live. Our claim is that this boundary remains useful when the agent is wrong.” |

If a stage fails, show its actual status and error. Do not turn a transport failure, cap refusal or incomplete fixture into a success badge. If hardware is disconnected, the request fails; there is no automatic software-signer fallback.

## What the three controls actually exercise

`GET /demo` reads current scenario state. `POST /demo/scenarios/normal`, `/attack`, or `/large` inserts explicitly labeled fixture inputs and returns current evidence. Controller authorization protects the POST routes. Existing packet and controller authorization routes complete the large case; this router never invokes a physical or simulated button.

Normal and large stages add a new matching PO, receipt and structured judge invoice for an existing, already provisioned EUR vendor. They run the ordinary invoice validation, payment policy, selected signer, simulated bank, and ledger. Structured invoices intentionally do not need a live extraction model. Optional natural-language explanations follow the application's configured LLM mode; this demo does not demonstrate PDF extraction accuracy.

The attack stage explicitly models an agent or desk that has already accepted forged business data. The trusted operator service, its signing keys and the operating system remain trusted; this does not model root compromise. Under the agent-runner lock, it temporarily substitutes an unverified host-side account for a known, provisioned EUR vendor and requests payment against a balanced payable. The original host account is restored in a `finally` block; the fake account is retained as a retired audit artifact. No human approval is invented and the chip enrollment never changes. Using a known vendor sends the wrong account to the FPGA instead of stopping at the host's unknown-vendor check. Its expected protection is account rejection, but the UI displays the response actually received. This is a boundary fault-injection test, not a forensic replay of the Pittsburg incident or proof that a live model was fooled by the exact original email.

The FPGA enforces committed-batch replay prevention, account rules, caps, holds and exact approvals. The desk and bank remain responsible for matching source documents and preventing economic obligations from being paid again under a different batch.

The generated world and scored replay are separate evaluation tools. The live scenarios do not read the answer key or inject “caught” scores. Live business-credit, sanctions and identity checks should only be claimed when a configured integration has actually returned evidence; a configured key or seeded vendor is insufficient.

## Track fit and official requirements

The [official Cal Hacks 13 page](https://cal-hacks-13-0.devpost.com/) names Hyphenate's track **The Hyphenated Enterprise: Autonomous Systems for Finance**. Its public prize listing gives $8,000/$5,000/$3,000 to the top three teams and expedited interviews for the top five.

The [official Hive prize page](https://hive.hackberkeley.org/prizes), backed by its [public prize feed](https://hive.hackberkeley.org/api/live/prizes), publishes four sponsor criteria: credible messy data and evidence; whether the system earns its business roles with limited permissions, an audit record and escalation; a fresh autonomous application or approach; and a memorable catch, recovery or defensible decision.

Hyphenate specifically asks for a video that starts with that memorable moment, a live demonstration that can accept a judge's scenario, a sentence naming the jobs filled, and evidence distinguishing real components from simulations. Open the **video** with the attack refusal and signer receipt, then explain how the desk reached it. The **live** three-minute sequence above establishes normal work before the attack and recovery. The seats sentence is: “PayGuard fills the invoice-checking, vendor-maintenance and payment-scheduling seats; the controller retains exception approval.”

The overall criteria are **Application, Intentionality, Functionality, Creativity, Technical Complexity**. For this project, show the finance workflow, a clear controller decision, a reliable working demonstration, the failure-tolerant design, and the hardware/host boundary. Those are our presentation choices, not additional official criteria.

The official submission page requires a project image, code repository and demonstration, with three minutes to pitch and two minutes for questions. It also asks for a meaningful video for asynchronous sponsor review and work completed during the hackathon. Verify the current submission deadline on that page. Prize outcomes are decided by the judges.
