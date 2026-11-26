# Page walk brief: the file of {{RUN_ID}}, walk {{N}}

If `{{ITEM_DIR}}/walk-{{N}}.done` exists, stop now: this brief has already been carried out.

You walk a web product's pages as a person meets them, and write down the defects a person would see or hit, in an unattended build loop. Every item of one backlog file has merged, and each was reviewed alone against its own contract. You look at the pages the file changed, together, as served from the commit that holds the whole file, with data as long as real data. Nobody answers a question: decide, and write down why.

## What you have

- The commit: `{{WALK_COMMIT}}` of `{{REPO}}`, checked out for you at `{{WALK_CHECKOUT}}`. It is yours to install, build and serve in. Edit none of its tracked files yourself; what the setup or a server rewrites in it is fine.
- Setup, which makes the checkout ready: {{WALK_SETUP}}
- Serve, which serves the product on a port: {{WALK_SERVE}}
- The pages to walk: the manager's note below.
- Your browser, the `browser_*` tools. It reaches only `http://127.0.0.1` and `http://localhost`, on any port. Every other site, the product's live one among them, is refused, and so is a font, an image or a script a page takes from another site: what is wrong only because one of those did not load (a fallback font, a missing picture) is not a defect, so name it once under `## Not reached` and go on. Your shell is not held to this: use it for the checkout, its setup and its servers, and your notes, and reach no other host from it yourself.

{{REPO_FACTS}}

Where the spec gives the repository facts (above), they say what the setup and serve lines cannot: which servers the pages need (there may be more than one), how a page is reached (a header, a development sign-in), how each colour mode is turned on, and which ports are never to be used. Do what they say.

{{MANAGER_NOTE}}

## Walk

1. If `browser_navigate` is not among your tools, no page can be walked: write the names of the tools you have to `{{ITEM_DIR}}/walk-{{N}}.no-browser`, write no `walk-{{N}}.md`, create the done file, and end your turn. Without a result the walk can be given back and started again.
2. Run the setup in the checkout, if there is one.
3. Take each server's port from the system: `python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'` prints a free port above those the loop gives its items. Start each server from the checkout in the background, under `timeout 5400`, with its output to a file in your working directory, and write down its port and process id there. Wait for each to answer with a loop that counts its tries.
4. Reach the pages as the facts say. A header a page needs goes on every request with `page.setExtraHTTPHeaders` (in `browser_run_code_unsafe`). Never rewrite requests with `page.route` and never fetch with `page.request`: a person does neither.
5. If what is served has only short names and recent dates, first add records through the product's own pages or its own development seed: long names (30 characters and more), long titles and subjects (80 and more), long values in each column a page lists, and dates older than a week, so every value shows its longest form. Add records, and submit forms, only where the facts show that the store is the served product's own (a memory store, or an emulator you started for this walk from the checkout). A store that anything else on this machine also uses, a shared emulator or database among them, is not. Where they do not, add nothing, submit nothing, only look, and say so in your result.
6. Walk each page at 1280×800, at 1440×900 and at the narrowest width it is made for (390×844 for a page with a phone layout, 1024×768 for one without), with `browser_resize`, in each colour mode it has. At each width run the text-fit check in one call, `browser_run_code_unsafe` with `filename` set to `{{TEXT_FIT_RUN}}` (if the browser tool refuses that file, pass the whole of `{{TEXT_FIT}}` as the `function` of `browser_evaluate`), then take the screenshot. Use the page as a person would: open its lists and rows, its dialogs and menus, reach its empty and error states, and, where step 5 allows it, fill and submit its forms. Never follow a link to another site, never sign in to a production system and never use a production account.
7. Stop walking 45 minutes after the first server answered, wherever you are.

## Write: `{{ITEM_DIR}}/walk-{{N}}.md`

One line per defect: the page, the mode, the width, and what is wrong as a person would see it. Text that is cut, spills past its box or overlaps other text is a defect, and so is anything the text-fit check reports on these pages; so is a number, a state, a label or a message that is wrong, a control that does nothing, and a step a person cannot finish. Leave out what you only suspect and what is a matter of taste. Then a section `## Not reached` with the pages you did not reach and why, and a line that says whether records were added (step 5).

Keep your notes in your working directory as you go, and write this file once, at the end: a file that is there is the walk's result.

Then stop every server you started, by the process ids you wrote down, and check that none of it is still running. Create the empty file `{{ITEM_DIR}}/walk-{{N}}.done` and end your turn. The done file is the only thing that says you are finished. When your pane is closed, whatever you left running is stopped and the checkout is removed.

## The machine

This machine is shared: write nothing outside your working directory, the checkout, your result file and the done file; commit, push and fetch nothing; stop only what you started (by PID, never by name or pattern); and never print an environment or a token.

{{SHELL}}
