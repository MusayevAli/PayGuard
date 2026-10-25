# SiliconSign protocol v1 — implementation contract

Threat boundary: agent laptop is untrusted. Kria boot/ARM bridge, provisioning authority,
controller approver and simulated bank are trusted. This development-board build does not
claim protection from privileged Kria software, JTAG, physical attacks or bitstream replacement.
No physical approval button is claimed. All amounts are unsigned 64-bit integer cents.

Every command and response is exactly 256 bytes, 64 little-endian uint32 words.
SV vector word i is bits i*32 +:32, byte i is bits i*8 +:8. HMAC keys and digests
use normal network/hex order (first byte in bits255:248). Command HMAC input is all
256 serialized bytes. A frame is transported as JSON {frame:hex,mac:hex} over newline
delimited serial or local simulation. No production payment network is invoked.

|Word|Meaning|
|---|---|
|0|magic command 0x53494c43; response 0x53494c52|
|1|version 1|
|2|opcode|
|3|sequence, strictly greater than role's last successful command, no wrap|
|4–7|128-bit session, raw bytes; INIT establishes session|
|8|vendor slot (0..63 in production)|
|9|immutable obligation slot (0..255 in production)|
|10|currency ISO numeric; only configured currency allowed (USD840 default)|
|11|flags: bit0 requires human exceptional approval; all other bits zero|
|12–13|amount cents|
|14|vendor version|
|15|approval TTL seconds for APPROVE_EXCEPTION (1..300)|
|16–23|account SHA256 bytes|
|24–31|invoice identity SHA256 bytes|
|32–33|Unix seconds for INIT / ADVANCE_DAY only|
|34–35|vendor per-day cap for ADD_VENDOR|
|36–37|global per-day cap for INIT|
|38–39|cumulative vendor spend threshold requiring exception approval, INIT|
|40|minimum bank-change hold seconds, INIT|
|41|max payments per vendor per day, INIT (1..65535)|
|42–43|restored global spent for INIT recovery|
|44–45|restored vendor spent for ADD_VENDOR recovery|
|46–53|SHA256 evidence digest; nonzero for vendor/obligation approval|
|54|restored vendor payment count for ADD_VENDOR|
|55|reserved zero|
|56–63|command reserved zero; response fields below|

Response echoes request except word0=response magic; word56=status;
word57=authorization counter (increments only successful PAY); words58–59=device
Unix seconds; words60–61=global spend; word62=vendor payment count;
word63=initialized. Entire response authenticated using the response-only key.

## Operations

1 INIT (admin HMAC): only while uninitialized, nonzero session, time, positive
global cap/threshold/min hold/max count; restore globalspent<=cap. Start internal clock.
2 ADD_VENDOR (admin): unused slot only, nonzero account/evidence, version=1,
configured currency, positive perday cap, restored spent/count in bounds. Approved
onboarding represented by admin authentication and evidence digest; live API checks
are external inputs, not fabricated inside FPGA.
3 CHANGE_BANK (admin): existing vendor, version=current+1 (no wrap), nonzero new
account different from previous, evidence nonzero. Sets hold_until=now+minimumhold;
preserves spent and count. Invalidates existing invoice bindings by version.
4 COMMIT_INVOICE (controller): unused invoice slot, existing vendor account/version
exact, configured currency, positive amount, nonzero invoice/evidence; reject duplicate
invoice hash across all slots. Stores immutable binding. It cannot overwrite paid slots.
5 APPROVE_EXCEPTION (controller): exact existing unpaid invoice/account/version/
amount/currency/hash binding; TTL1..300. Sets approval deadline now+TTL. Approval
cannot waive bank hold, caps, currency or duplicate restrictions.
6 PAY (unsigned agent): exact invoice binding, approved vendor account/version,
cooldown expired, amount positive, caps checked overflow-safe, count below limit.
Exception required if invoice.flags bit0 OR vendor_spent+amount > threshold. Approval
must be present and now < deadline. Atomically mark invoice consumed, consume approval,
reserve spending/count, increment auth counter, last payment sequence. Never release
reservation merely because gateway times out. No release/cancel operation in v1.
7 DISABLE_VENDOR (admin): disables slot permanently for this session.
8 ADVANCE_DAY (admin): timestamp day strictly greater than current budget day AND
device clock has itself reached requested day (timestamp<=now). Clears daily counters
and spent only, never paid flags/invoices. Never moves internal time forward.
9 STATUS (unsigned): read-only signed response, no sequence advance.
10 LOCK (admin): deinitializes / locks payment requests; no state clearing or reinit
until hardware reset. Require separate locked_latched flag to block INIT after LOCK.
11 REBIND_INVOICE (controller): existing unpaid invoice only; same invoice hash,
vendor, amount, currency and flags; nonzero fresh evidence digest. New account/version
must equal the current enabled vendor and differ from the existing invoice binding.
Updates account/version only and clears exception approval. Does not waive cooldown,
change counters, erase history, or produce new accounting recognition entries. Gateway
updates an unpaid liability binding through its separately authorized controller path.

INIT is the only clock-setting command; internal seconds advance at CLK_HZ parameter.
Day rollovers are explicit authenticated operations and can never be requested early.
PAY rejects with DAY_ROLLOVER if now/86400 != budgetday, requiring ADVANCE_DAY.
All privileged operations require valid role-specific HMAC; use distinct ADMIN_KEY,
CONTROLLER_KEY, RESPONSE_KEY. Controller operations are 4,5,11. Bad auth never mutates state. Admin cannot commit invoices;
controller cannot add/change vendors. Unrecognized fields/overflow reject, never truncate.

Status enum: OK0 BAD_FRAME1 AUTH2 LOCKED3 SESSION4 SEQUENCE5 RANGE6 EXISTS7
VENDOR8 ACCOUNT9 INVOICE10 AMOUNT11 CURRENCY12 COOLDOWN13 DAILY_CAP14
VENDOR_CAP15 APPROVAL16 PAID17 CAPACITY18 TIME19 FLAGS20 DISABLED21
VERSION22 ZERO23 COUNT24 DAY_ROLLOVER25 DUPLICATE26 OVERFLOW27.

## Capacity and AXI discovery

Production RTL defaults and the KV260 build both use `NV=64`, `NI=256`. An obligation
can bind an independently approved batch of invoices; its identity must cover that
entire immutable batch. The hardware does not accept an unbounded invoice list.
Slots remain occupied after payment and daily rollover. Vendor index 64 and
obligation index 256 return `RANGE`; reusing an occupied slot returns `EXISTS`.
The host must report capacity exhaustion explicitly instead of recycling slots or
silently resetting the FPGA. Reset/recovery requires reconciliation of prior spend,
consumed invoices and outstanding authorizations.
Payload RAM is not erased on reset. Slot validity and approval flags are reset, and
each accepted new vendor/obligation writes its complete payload before it becomes
usable. The discarded contents cannot authorize payments. This is logical session
invalidation, not physical memory sanitization.

The AXI4-Lite window starts at physical address `0xA0000000` and spans 4 KiB.

|Offset|Access|Meaning|
|---|---|---|
|`0x000`|write|`1` starts one request; `2` acknowledges a completed response|
|`0x004`|read|bit0 busy, bit1 completion pending, bit2 distinct nonzero keys configured|
|`0x008`|read|IP identity `0x53494c31`|
|`0x00c`|read|capacity: upper16 = obligation slots, lower16 = vendor slots|
|`0x010`|read|compiled cycles per second: `50000000` in the 50 MHz production image|
|`0x100..0x1fc`|read/write|256-byte command, 64 little-endian words|
|`0x200..0x21c`|read/write|32-byte command HMAC|
|`0x300..0x3fc`|read|256-byte authenticated response|
|`0x400..0x41c`|read|32-byte response HMAC|

The production capacity word is `0x01000040`. Capability writes return `SLVERR`.
Earlier images have no capability register; a missing, zero or insufficient value
must fail startup rather than be guessed from the IP identity. The UART `hello`
response may relay these values but remains informational, never an authorization.
This adds discovery only: command/response framing and all HMAC inputs stay unchanged.
The divider register is also read-only. Its value must match the required image,
and the actual PL0 clock must be 49.5–50 MHz. A faster clock can shorten holds and must
be rejected; a slightly slower accepted clock makes the 50,000,000-cycle second
conservatively longer. Vivado's PS clock, RTL divider and Linux overlay all request
the same nominal 50 MHz. Hardware waiting periods and approval TTLs are unchanged.

## Standalone gateway settlement and recovery

The following describes the `hardware/python/siliconsign` standalone gateway and
recovery coordinator. PayGuard's `bank/` and `signer/` integration must establish its
own transaction and recovery guarantees; this protocol alone does not provide them.

Trusted bank verifies full response MAC, magic/version/status=OK/op=PAY, active session,
exact submitted fields, independently approved invoice; SQLite transaction uniquely
consumes (session,authorizationcounter) AND invoice identity across sessions. Same
authorization retry returns prior receipt, never a second debit. Recognition Dr expense
/Cr AP; settlement Dr AP/Cr cash, in one transaction with authorization consumption.

Trusted bridge journals commands and signed results durably before releasing a response.
On any restart, unknown outstanding authorization or persistence failure, stop serving
PAY. Rotate session with gateway, recover consumed invoices/reservations and daily counters
from trusted journal, then provision a fresh boot. Old sessions remain permanently revoked.
Do not replay INIT into an active session. Persistence is trusted software on existing
microSD, not tamper-resistant FPGA NVRAM. No rollback-resistance claim against board admin.

## Module interfaces

policy_engine #(CLK_HZ=50000000,NV=64,NI=256): clk,rst_n;
input cmd_valid,input [2047:0] cmd,input auth_ok;
output cmd_ready,output reg resp_valid,output reg[2047:0]resp.
cmd_valid single pulse when ready. auth_ok is wrapper's verified role HMAC.
One request at a time, wrapper holds responses until host consumes them.
The policy captures the complete command and authentication result at acceptance,
then keeps `cmd_ready` low until completion. It ignores new input values and valid
pulses while busy. A synchronous block-RAM read supplies the selected identity;
COMMIT scans all 256 slots before final validation. At nominal 50 MHz the scan adds
about 5.2 microseconds. Other operations use one selected read. The seconds clock
continues while busy; all policy mutation occurs once at final validation. Reset
aborts an in-flight request and clears validity flags without erasing payload RAM.

hmac_sha256: clk,rst_n,start,key[255:0],message[2047:0],busy,done,digest[255:0].

siliconsign_core: clk,rst_n,start,command[2047:0],command_mac[255:0];
outputs busy,done,response[2047:0],response_mac[255:0]. Keys module parameters, nonzero
generated outside version control; test keys used only in simulation fixtures.
