MOVIE FILTER
============

Movie Filter finds swear words in your TV shows and movies. It reads each
video's subtitle file, listens to the audio at those moments to find exactly
when each word is spoken, and writes an EDL file that tells your player to mute
them. It also censors the subtitle with asterisks.

For each video, the folder ends up holding:

    Silo_S01E01.mkv
    Silo_S01E01.en.srt     the chosen subtitle, swear words replaced with ****
    Silo_S01E01.edl        the mute list
    Trash/                 the other subtitles, the uncensored original and a
                           report of every timestamp (safe to delete)


WHAT YOU NEED
-------------

- A Mac (Apple Silicon, macOS 14 or newer) or a Windows PC.
- Python 3.12, 3.13 or 3.14. Python 3.15 and later are NOT supported yet,
  because the speech recognizer hasn't published builds for them. Python 3.13
  is the recommended choice, and it can be installed alongside other versions.
- ffmpeg. The launcher installs it for you (Homebrew on Mac, winget on Windows).
- An internet connection the first time, to download the libraries and the
  speech model.


INSTALLING PYTHON 3.13
----------------------

Mac, with Homebrew (https://brew.sh):

    brew install python@3.13 python-tk@3.13

The second package adds the window support (Tkinter), which Homebrew leaves out
by default. Alternatively use the macOS installer from
https://www.python.org/downloads/ (scroll to "Looking for a specific release?").

Windows: install Python 3.13 from https://www.python.org/downloads/ and keep the
"py launcher" option ticked.

You don't need to change anything afterwards. The launcher looks for Python
3.13 first, then 3.12, then 3.14, and ignores unsupported versions.


RUNNING IT
----------

1. Double-click "Movie Filter.command" (Mac) or "Movie Filter.bat" (Windows).
   On a Mac, right-click it and choose Open the first time.
   The first run sets things up in a hidden ".venv" folder, which takes a few
   minutes.
2. Choose your folder of videos.
3. Check the subtitle choice for each episode. Choices it's unsure about are
   marked "Please check". Double-click a different subtitle file to use it.
4. Tick "Preview only" and run one episode first. That reports what it finds
   without changing any files.
5. Untick "Preview only" and press Start.

Episodes that already have an .edl file are skipped, so you can rerun the
folder after adding new episodes.


WHICH ACCURACY SETTING TO USE
-----------------------------

The Accuracy setting chooses the speech model that listens for the words. Hover
over it in the window for a reminder.

  medium.en   RECOMMENDED, and the default. The best balance on a modern
              Mac or PC. Much better than the smaller models at catching words
              spoken quickly, quietly, mumbled or over music.

  large-v3    The most accurate and the slowest. Worth trying if the report in
              Trash shows many "estimated" times at medium.en.

  small.en    Faster, but misses more words. A missed word is placed using the
  base.en     words around it, which gives a longer, less precise mute.

Speed matters less than you might expect, because Movie Filter only listens to
the few seconds around each line that contains a swear word, not the whole
episode. Each setting downloads its model the first time you choose it, so
expect a pause then.

On a Windows PC with an NVIDIA graphics card, the command-line option
--device cuda is faster, but needs NVIDIA's CUDA libraries installed.


MUTE OR SKIP
------------

Mute is the default: the picture keeps playing and only the sound drops out.
Skip jumps over the moment instead, which can look choppy. Choose it in the
window, or use --skip on the command line.

Your player must support EDL files (Kodi and MPlayer do, for example). The EDL
must sit next to the video with the same name.


THE WORD LIST
-------------

swear_words.txt is deliberately broad. For example "Dick" as a name and "hell"
in any sense are filtered, along with phrases like "oh my God". Edit it to suit
you:

    damn        matches the word on its own
    damn*       also matches damned, damnit
    *hole       matches words ending in "hole"
    !scunthorpe never filter this, even if it contains a listed word

Lines starting with # are ignored.


WHAT IT CAN MISS
----------------

- Swears that aren't in the subtitle. It only listens near subtitle lines that
  contain a swear word, so a softened subtitle line far from any other swear
  gets through.
- Subtitles inside the MKV. Only separate .srt files are read, so an episode
  without one is left unchanged.
- Check a few timestamps from the report against the show after your first
  run. If words are being clipped, try a higher accuracy setting, or increase
  --pad on the command line (default 0.15 seconds before each word; 0.10 is
  always added after).


COMMAND LINE
------------

From the Movie Filter folder, after the launcher has run once:

    Mac:     .venv/bin/python movie_filter.py "/path/to/TV" --auto
    Windows: .venv\Scripts\python.exe movie_filter.py "D:\TV" --auto

Options:
    --auto           no window; use the recommended subtitle for each video
    --dry-run        report only; change no files
    --skip           write skip entries instead of mutes
    --force          also redo videos that already have an .edl (don't use this
                     on episodes whose subtitle is already censored: it would
                     find no swears and write an empty EDL)
    --model NAME     base.en, small.en, medium.en (default) or large-v3
    --device cuda    use an NVIDIA graphics card
    --pad SECONDS    extra time muted before each word (default 0.15)
    --dictionary F   use a different word list


UPDATING FROM AN EARLIER VERSION
--------------------------------

Copy the new files over the old ones in your Movie Filter folder. The launcher
notices the new requirements.txt and updates its libraries on the next run, so
you don't need to delete the .venv folder. If you edited swear_words.txt, keep
your copy.

Episodes made by the very first version (which skipped instead of muting) have
EDL lines ending in 0. To make them mute, change the third column of each EDL
line from 0 to 1.


TROUBLESHOOTING
---------------

"Movie Filter needs Python 3.12, 3.13 or 3.14"
    Install Python 3.13 as described above, then run the launcher again.

"has no window support"
    Mac: run the brew install python-tk@... command the message shows.

"Failed to build 'av'" or "CompileError: av/logging.pyx" during setup
    pip couldn't find a ready-made library build for your Python, usually
    because the Python is too new or too old. Install Python 3.13, delete the
    hidden .venv folder (in Finder, Cmd+Shift+. shows hidden files), and run
    the launcher again. Current launchers choose a supported Python and refuse
    to compile, so this should not reappear.

"open() got an unexpected keyword argument 'metadata_errors'"
    Fixed in this version: PyAV is held below version 19, and the audio is
    handed to the recognizer directly. Copying in the new files fixes it.

"zsh: permission denied: ..." when typing in Terminal
    You entered a folder path on its own. Type cd and a space first, then
    drag the folder into the window.

Anything else
    The launcher can always start over cleanly: delete the .venv folder and run
    it again.
