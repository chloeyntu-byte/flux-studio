import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from video_animation import Animation, encode_animation
from video_chart import _plot


class AnimationTest(unittest.TestCase):
    size = (5, 3)
    red = b'\xff\x00\x00' * 15
    green = b'\x00\xff\x00' * 15
    blue = b'\x00\x00\xff' * 15

    def test_frame_order_and_single_ending_hold(self):
        frames = [self.red, self.green, self.blue]
        with tempfile.TemporaryDirectory() as temporary:
            movie = Path(temporary) / 'movie.png'
            self.assertEqual(encode_animation(movie, self.size, 8, iter(frames), end_seconds=3), 3)

            with Animation(movie) as animation:
                self.assertEqual(animation.size, self.size)
                self.assertEqual(animation.delays, [0.125, 0.125, 3])
                for index, rgb in enumerate(frames):
                    self.assertEqual(animation.ppm_frame(index), b'P6\n5 3\n255\n' + rgb)
                    self.assertTrue(animation.png_frame(index).startswith(b'\x89PNG\r\n\x1a\n'))

    def test_cancellation_keeps_previous_movie_and_removes_partial(self):
        cancel = threading.Event()

        def cancelled_frames():
            yield self.green
            cancel.set()
            yield self.blue

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            movie = directory / 'movie.png'
            encode_animation(movie, self.size, 8, [self.red, self.blue])
            original = movie.read_bytes()

            with self.assertRaises(InterruptedError):
                encode_animation(movie, self.size, 8, cancelled_frames(), cancel_event=cancel)

            self.assertEqual(movie.read_bytes(), original)
            self.assertEqual(list(directory.iterdir()), [movie])

    def test_truncated_animation_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            movie = Path(temporary) / 'movie.png'
            encode_animation(movie, self.size, 8, [self.red, self.blue])
            movie.write_bytes(movie.read_bytes()[:-5])

            with self.assertRaises(ValueError):
                Animation(movie)


class ObjectiveChartTest(unittest.TestCase):
    def test_target_chart_reports_best_feasible_absolute_error_in_ph(self):
        frequency = 20e9
        goal = {'metric': 'Lp', 'direction': 'target', 'frequency_hz': frequency, 'target': .270}
        rows = [
            {'index': index, 'id': str(index), 'generation': generation, 'status': 'completed',
             'feasible': feasible, 'metrics': [{'frequency_hz': frequency, 'Lp': inductance}]}
            for index, generation, feasible, inductance in [(1, 0, True, .261), (2, 1, False, .270),
                                                            (3, 1, True, .2697)]
        ]
        drawing = Mock()
        drawing.textlength.side_effect = lambda label, font=None: len(label) * 7

        _plot(drawing, (0, 0, 1200, 350), rows, goal, set(), (None, None))

        labels = [call[0][1] for call in drawing.text.call_args_list]
        self.assertIn('Lp error (pH)   |   Target 0.27 nH   |   20 GHz', labels)
        self.assertIn('First gen best 9   /   Run best 0.3', labels)


if __name__ == '__main__':
    unittest.main()
