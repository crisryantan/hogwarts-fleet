# Standing orders

No standing orders yet. Merges and deploys can never be standing orders.

Closing stays a gate, not a standing order. Ryan closes with "Mischief managed <task-id>", or with "Mischief managed everything" typed as the whole message: that closes every task that is reviewed (PASS), dropped, or merged with its after-merge checks proven, and refuses by name, with the reason and what to type next, every task in flight (with a desk, in review, awaiting a verdict, a live build) and every task with an open desk question. A go task whose last build has closed moves on by itself, only on a build's proof from the closer Ryan switched on.

These are written in words for people to read. Code never parses this file: a switch the fleet acts on lives in the office, where no desk can write. An order you switch on there is worth writing here too, for example:

- Follow-up replies on PRs the review loop opened, while `pr-followup` is on.
- McGonagall's typed next steps after a go (a fix round, the next review, a read-only data question, a draft PR after a recorded PASS, a one-line note), while `auto-orchestrate` is on.
