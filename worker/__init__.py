"""Jarvis background worker: runs jobs while the desktop app is closed.

Headless on purpose: nothing in this package imports the microphone, speaker, wake-word
detector or any UI code (tests/test_worker.py enforces that).
"""
