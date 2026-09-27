if __name__ == "__main__":
    import multiprocessing

    # Required in a packaged .exe: analysis worker processes start the same
    # executable, which must run the worker instead of opening another window.
    multiprocessing.freeze_support()
    # Imported here so spawned analysis processes, which re-run this module
    # as ``__mp_main__``, do not load the Qt UI.
    from tesla_viewer.main_window import main

    main()
