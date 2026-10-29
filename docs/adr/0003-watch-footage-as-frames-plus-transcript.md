# The Watcher reads sampled frames plus a transcript, not raw video

Claude reads images and text, not video, so the Watcher works on frames sampled from the Source Footage (on a fixed interval plus scene changes) and a speech transcript, both sent to Claude. We chose this over adding a separate video-understanding model to keep the whole team on one model, keep cost low for short trailers, and keep the mechanism simple enough to explain. A reader should not "fix" this by switching to a video model without weighing that trade-off again.
