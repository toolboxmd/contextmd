This is a hook delivery test. Hooks may add text that starts with "contextmd canary" and carries tokens like CMDCANARY-XXXX-1234. Whenever you can see such text, write every token you see, exactly, in your next message before calling any tool. If you see none, write "no canary".

Do these steps one at a time. Use one shell command per tool call, and write a short message before each tool call:

1. Run `echo canary-step-1`.
2. Run `ls /nonexistent-canary-dir`. It fails; that is expected.
3. Run `echo canary-big`.
4. Run `echo canary-deny`. If it is blocked, follow the reason, then run it again.
5. Reply with the single word: done.
