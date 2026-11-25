# EBG WhatsApp Agent

A separate app (not part of the Task Assigner) that gives each brand an AI assistant on WhatsApp.
The team trains each agent, tests it in a practice chat, and then connects it to the brand's
WhatsApp Business number. Replies are written by Claude (Anthropic).

## What's in the app

- **Agents:** one per brand. Each agent has four tabs:
  - **Train:** brand profile (what the brand does, tone, language, greeting, rules, when to
    hand over to a person), knowledge base (FAQs, products, prices, policies) and example replies
    ("customer says X, reply Y").
  - **Test chat:** chat as a customer; nothing is sent on WhatsApp. **Correct this reply** saves
    your version as a new example, and the agent uses it straight away.
  - **Conversations:** real WhatsApp chats. When a customer needs a person, the agent says someone
    will follow up, stops replying in that chat and marks it *Needs team*. Teammates can reply on
    WhatsApp from here and hand the chat back to the agent.
  - **Connect WhatsApp:** step-by-step setup with Meta's WhatsApp Cloud API.
- **Team:** Admins add teammates (email + temporary password), change roles and remove people.

### Adding files to the knowledge base

In **Train → Knowledge base**, click **Attach files** or drop files onto it (several at once,
up to 3 MB each). Each file becomes a knowledge entry you can open, check and correct.

| File | How it's read |
|---|---|
| PDF, images (JPG, PNG, WEBP, GIF) | Claude reads them, including scans and photos of menus or price lists, and writes out the facts. Takes up to a minute or two. |
| Word `.docx`, Excel `.xlsx`, PowerPoint `.pptx` | Text and tables are read directly. |
| TXT, CSV, MD, JSON, HTML, XML, RTF and other text files | Read as they are. |

Not supported: audio, video, archives (zip) and old `.doc`/`.xls`/`.ppt` files (save those as
`.docx`/`.xlsx`/`.pptx` or PDF first). Only the text is stored, not the original file. Each entry
keeps up to 50,000 characters (split longer files), and an agent's whole knowledge base up to
400,000.

| | Admin | Member |
|---|---|---|
| Train agents, test chat, reply to customers | ✓ | ✓ |
| Create or delete agents, connect WhatsApp, turn auto-replies on or off | ✓ | |
| Manage the team | ✓ | |

The first account created on a fresh install becomes the Admin. Any email address can be used.

## Run it on a PC (for training and testing)

Double-click **`start-server.bat`** and keep its window open. It installs the Anthropic package
and opens <http://localhost:8095>. Data is stored in `data.json` in this folder.

Set the Anthropic key first (PowerShell, then start the server from the same window):

```powershell
$env:ANTHROPIC_API_KEY="sk-ant-..."; python server.py --open
```

WhatsApp can't reach a PC, so going live needs the Vercel setup below.

## Put it online (Vercel)

1. Push this folder to its **own** GitHub repository and import it as a **new** Vercel project
   (Framework Preset **Other**, no build command).
2. **Storage → Create Database → Upstash for Redis**, connected to this project for all
   environments. (It can share the Task Assigner's database: this app keeps its data under its
   own `ebgwa:` keys.)
3. **Settings → Environment Variables:** add `ANTHROPIC_API_KEY` (create one at
   <https://platform.claude.com> → API keys).
4. Redeploy. Open the site; the first sign-up becomes the Admin.
5. For each brand: create the agent, train it, then follow its **Connect WhatsApp** tab.

### Optional settings

| Variable | Default | What it does |
|---|---|---|
| `AGENT_MODEL` | `claude-opus-5-5` | Claude model that writes replies |
| `REDIS_PREFIX` | `ebgwa` | Key prefix in Redis |
| `WHATSAPP_GRAPH_VERSION` | `v21.0` | Meta Graph API version |

## Good to know

- WhatsApp only allows free-form replies within 24 hours of the customer's last message.
- `vercel.json` gives each request up to 300 seconds, so long PDFs can finish reading. That needs
  Vercel's Fluid Compute, which is on by default for new projects; if a deploy complains about the
  duration, turn it on under *Settings → Functions*.
- Each agent keeps its latest 200 conversations (60 messages each).
- Incoming messages are only accepted when signed with the Meta app secret; access tokens and
  app secrets are never sent to the browser.
- If Claude declines to answer a message, the request is retried on a backup model automatically.
