"""Local, streaming playback for Flux's portable optimization videos."""

import fieldwork_tk as fw
import argparse
from itertools import accumulate
from time import monotonic
import tkinter as tk
from tkinter import ttk, messagebox

from video_animation import Animation


class VideoPlayer(tk.Toplevel):
    def __init__(self, parent, path):
        self.animation = Animation(path)
        try:
            super().__init__(parent)
        except tk.TclError:
            self.animation.close()
            raise
        self.title('Flux Studio — Optimization video')
        fw.apply(self, product='flux')
        fw.header(self, 'Flux Studio', 'Optimization video').pack(fill='x')
        self.index = 0
        self.timer = None
        self.playing = False
        self.setting_slider = False
        self.times = [0.0] + list(accumulate(self.animation.delays))
        width, height = self.animation.size
        usable_width = max(1, self.winfo_screenwidth() - 60)
        usable_height = max(1, self.winfo_screenheight() - 130)
        self.divisor = max(1, (width + usable_width - 1) // usable_width,
                           (height + usable_height - 1) // usable_height)
        self.picture = ttk.Label(self, anchor='center')
        self.picture.pack(fill='both', expand=True)
        controls = ttk.Frame(self, padding=(12, 8))
        controls.pack(fill='x')
        self.play = ttk.Button(controls, text='Play', width=8, command=self.toggle)
        self.play.pack(side='left')
        self.slider = ttk.Scale(controls, from_=0, to=len(self.animation.delays)-1, command=self.seek)
        self.slider.pack(side='left', fill='x', expand=True, padx=12)
        self.counter = ttk.Label(controls)
        self.counter.pack(side='right')
        self.bind('<space>', lambda _: self.toggle())
        self.protocol('WM_DELETE_WINDOW', self.destroy)
        try:
            self.display_frame()
        except (OSError, ValueError, tk.TclError):
            self.destroy()
            raise

    def display_frame(self):
        picture = tk.PhotoImage(master=self, data=self.animation.ppm_frame(self.index), format='ppm')
        self.photo = picture if self.divisor == 1 else picture.subsample(self.divisor)
        self.picture.configure(image=self.photo)
        self.setting_slider = True
        self.slider.set(self.index)
        self.setting_slider = False
        self.counter.configure(text='{:.2f} / {:.2f} s   ·   {} / {}'.format(
            self.times[self.index], self.times[-1], self.index+1, len(self.animation.delays)))

    def seek(self, value):
        if self.setting_slider:
            return
        index = max(0, min(len(self.animation.delays) - 1, round(float(value))))
        if index != self.index:
            self.index = index
            self.draw_or_stop()

    def draw_or_stop(self):
        started = monotonic()
        try:
            self.display_frame()
        except (OSError, ValueError, tk.TclError) as exc:
            self.stop()
            messagebox.showerror('Play Video', str(exc), parent=self)
            return
        self.schedule(monotonic() - started)

    def schedule(self, drawing_seconds=0):
        if self.timer is not None:
            self.after_cancel(self.timer)
            self.timer = None
        if self.playing:
            delay = self.animation.delays[self.index] - drawing_seconds
            self.timer = self.after(max(1, round(delay * 1000)), self.advance)

    def advance(self):
        self.timer = None
        if self.index + 1 == len(self.animation.delays):
            self.stop()
            return
        self.index += 1
        self.draw_or_stop()

    def toggle(self):
        if self.playing:
            self.stop()
        else:
            if self.index + 1 == len(self.animation.delays):
                self.index = 0
            self.playing = True
            self.play.configure(text='Pause')
            self.draw_or_stop()

    def stop(self):
        self.playing = False
        self.play.configure(text='Play')
        self.schedule()

    def destroy(self):
        if self.timer is not None:
            self.after_cancel(self.timer)
            self.timer = None
        self.animation.close()
        super().destroy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('video', help='A Flux optimization-history.png video')
    args = parser.parse_args()
    try:
        root = tk.Tk()
        root.withdraw()
        try:
            player = VideoPlayer(root, args.video)
            player.bind('<Destroy>', lambda event: root.quit() if event.widget is player else None)
            root.mainloop()
        finally:
            root.destroy()
    except (OSError, ValueError, tk.TclError) as exc:
        parser.exit(1, 'Playback failed. {}\n'.format(exc))


if __name__ == '__main__':
    main()
