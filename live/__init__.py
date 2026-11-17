"""Live watcher: follows a live channel, keeps a rolling recording, reads chat and cuts a clip whenever a moment spikes.

    python -m live.watcher --watch <watch_id>          # a watch created in the admin panel
    python -m live.watcher --channel somechannel --local out/   # no database: clips go to a folder

See README.md for how it works and how to run it.
"""
