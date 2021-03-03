from google.cloud import speech

try:
    # additional explicit imports for compatibility with
    # google-cloud-speech=1.3.2 for python2
    from google.cloud.speech import enums
    from google.cloud.speech import types
except ImportError:
    pass

import pyaudio
from six.moves import queue

from TextBatchProcessor import TextBatchProcessor

# Audio recording parameters
SAMPLE_RATE = 16000
CHUNK = int(SAMPLE_RATE / 10)  # 100ms
PA_FORMAT = pyaudio.paInt16


class AudioStreamer(object):
    """Opens a recording stream as a generator yielding the audio chunks."""

    def __init__(self, rate=SAMPLE_RATE, chunk=CHUNK, pa_format=PA_FORMAT):
        self._rate = rate
        self._chunk = chunk
        self._pa_format = pa_format

        # Create a thread-safe buffer of audio data
        self._buff = queue.Queue()
        self.closed = True

    def __enter__(self):
        self._audio_interface = pyaudio.PyAudio()
        self._audio_stream = self._audio_interface.open(
            format=self._pa_format,
            # The API currently only supports 1-channel (mono) audio
            # https://goo.gl/z757pE
            channels=1,
            rate=self._rate,
            input=True,
            frames_per_buffer=self._chunk,
            # Run the audio stream asynchronously to fill the buffer object.
            # This is necessary so that the input device's buffer doesn't
            # overflow while the calling thread makes network requests, etc.
            stream_callback=self._fill_buffer,
        )

        self.closed = False

        return self

    def __exit__(self, type, value, traceback):
        self._audio_stream.stop_stream()
        self._audio_stream.close()
        self.closed = True
        # Signal the generator to terminate so that the client's
        # streaming_recognize method will not block the process termination.
        self._buff.put(None)
        self._audio_interface.terminate()

    def _fill_buffer(self, in_data, frame_count, time_info, status_flags):
        """Continuously collect data from the audio stream, into the buffer."""
        self._buff.put(in_data)
        return None, pyaudio.paContinue

    def stream(self):
        while not self.closed:
            # Use a blocking get() to ensure there's at least one chunk of
            # data, and stop iteration if the chunk is None, indicating the
            # end of the audio stream.
            chunk = self._buff.get()
            if chunk is None:
                return
            data = [chunk]

            # Now consume whatever other data's still buffered.
            while True:
                try:
                    chunk = self._buff.get(block=False)
                    if chunk is None:
                        return
                    data.append(chunk)
                except queue.Empty:
                    break

            yield b"".join(data)


class SpeechRecognizer(object):
    def __init__(
        self,
        rate=SAMPLE_RATE,
        chunk=CHUNK,
        lang="en-US",
        text_batcher=None,
        text_batch_processor=None,
    ):

        self._rate = rate
        self._chunk = chunk

        self._lang = lang
        self._punctuation = True
        self._text_batcher = text_batcher

        self._text_batch_processor = text_batch_processor
        if text_batch_processor is None:
            self._text_batch_processor = TextBatchProcessor(lang=lang)

        self._phrases = (
            getattr(self._text_batch_processor, "context_phrases", [])
            + getattr(self._text_batch_processor, "action_phrases", [])
            + getattr(self._text_batch_processor, "expression_phrases", [])
            + getattr(self._text_batch_processor, "animation_phrases", [])
        )

    def _get_speech_client_and_config(self):
        client = speech.SpeechClient()

        recognition_config = dict(
            sample_rate_hertz=self._rate,
            language_code=self._lang,
            max_alternatives=1,
            enable_automatic_punctuation=self._punctuation,
            use_enhanced=True,
        )

        try:
            speech_contexts = speech.SpeechContext(phrases=self._phrases)
            encoding = speech.RecognitionConfig.AudioEncoding.LINEAR16
            RecognitionConfig = speech.RecognitionConfig
            StreamingRecognitionConfig = speech.StreamingRecognitionConfig

        except AttributeError:
            # Python2 and speech 1.3.2 compatibility
            speech_contexts = types.SpeechContext(phrases=self._phrases)
            encoding = enums.RecognitionConfig.AudioEncoding.LINEAR16
            RecognitionConfig = types.RecognitionConfig
            StreamingRecognitionConfig = types.StreamingRecognitionConfig

        config = RecognitionConfig(
            encoding=encoding, speech_contexts=[speech_contexts], **recognition_config
        )

        streaming_config = StreamingRecognitionConfig(
            config=config, interim_results=True
        )

        return client, streaming_config

    def transcribe(self):

        client, streaming_config = self._get_speech_client_and_config()

        with AudioStreamer(self._rate, self._chunk) as audio_streamer:

            try:
                StreamingRecognizeRequest = speech.StreamingRecognizeRequest
            except AttributeError:
                # Python2 and speech 1.3.2 compatibility
                StreamingRecognizeRequest = types.StreamingRecognizeRequest

            requests = (
                StreamingRecognizeRequest(audio_content=content)
                for content in audio_streamer.stream()
            )

            responses = client.streaming_recognize(streaming_config, requests)

            # Now, put the transcription responses to use.
            self._handle_responses(responses)

    def _handle_responses(self, responses):

        for response in responses:
            if response.results and (
                len(response.results) > 1 or response.results[0].is_final
            ):

                result = response.results[0]
                if result.alternatives:
                    text = result.alternatives[0].transcript
                    confidence = int(100 * result.alternatives[0].confidence)

                    batch = None
                    if self._text_batcher:
                        batch = self._text_batcher.get_batch(
                            text, reset=result.is_final
                        )
                    elif result.is_final:
                        batch = [text]
                    if batch:
                        self._text_batch_processor.process(batch, reset=result.is_final)

                    if result.is_final:
                        print("--Final--\n", text, "\n---\n")


if __name__ == "__main__":
    from TextBatcher import TextBatcher
    from TextBatchPublisher import TextBatchPublisher

    import rospy

    rospy.init_node("speech_recognizer")

    while True:
        try:
            # lang = "en-US"
            lang = "en-IN"

            text_batcher = None
            # text_batcher = TextBatcher()

            # text_batch_processor = None
            text_batch_processor = TextBatchPublisher(lang=lang)

            SpeechRecognizer(
                lang=lang,
                text_batcher=text_batcher,
                text_batch_processor=text_batch_processor,
            ).transcribe()

        except Exception as exception:
            print(exception)
    # rospy.spin()
