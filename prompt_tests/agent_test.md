# Benchmark

This benchmark validates real end-to-end execution under a non-trivial workflow.

It checks whether the agent can:

- Run parallel sub-agents while continuing its own main thread.
- Download and verify assets.
- Create and edit local files based on live progress.
- Execute native OS commands and adapt if the first method fails.
- Clean temporary artifacts and produce a final technical summary.

Tested on macOS, Windows, and Linux.

Copy and paste this to the model to test that everything is working correctly:

"I need a new vibe for my desktop. Please change my wallpaper to this image: 'https://wallpapercave.com/download/night-city-4k-desktop-wallpapers-wp10920086'.

To keep things clean and efficient, delegate the downloading and verifying of the image into a temporary folder to a parallel sub-agent.

While that is happening, create a local text file named vibe_check.txt and write down your initial strategy for changing the wallpaper on my current machine.

Once the image is downloaded, determine how to set it as my background natively. After you succeed, edit vibe_check.txt to append the exact scripts or commands that actually worked.

Check if the wallpaper was actually changed to the new one; if not, try an alternative method.

Finally, clean your SoT (State of Thought), delete the downloaded image file so no trash is left behind, read the final contents of your text file, and give me a short summary including: my exact OS environment, the technical method you ended up using, and confirmation that the cleanup was successful."
