import os
import subprocess

def split_audio_video(file_path):
    # Separate the original file name and extension
    base_name, ext = os.path.splitext(file_path)
    
    # Set the audio and video file names
    audio_output = f"{base_name}_audio.mp3"
    video_output = f"{base_name}_video{ext}"
    
    # Set up FFmpeg commands
    audio_command = ["ffmpeg", "-y", "-i", file_path, "-q:a", "0", "-map", "a", audio_output]
    video_command = ["ffmpeg", "-y", "-i", file_path, "-an", video_output]
    
    # Extract audio
    subprocess.run(audio_command, check=True)
    
    # Extract video
    subprocess.run(video_command, check=True)

    print(f"Audio saved to: {audio_output}")
    print(f"Video saved to: {video_output}")
    return [audio_output, video_output]


def replace_audio_from_video(video_file: str, audio_file: str, output_file=None):
    """
    Replaces the audio in a video file with a new audio file and
    creates the result file by appending '_merged' to the original file name.

    Args:
        video_file (str): The path to the video file whose audio will be replaced.
        audio_file (str): The path to the new audio file to use.
        output_file (str): The output file path.
    """
    # Separate the original video file name and extension
    base_name, ext = os.path.splitext(video_file)
    
    # Create the output file name
    if output_file is None:
        output_file = f"{base_name}_merged{ext}"
    
    # Build the ffmpeg command.
    command = [
        'ffmpeg',
        '-y',
        '-i', video_file,  # Input video file
        '-i', audio_file,  # Input audio file
        '-c:v', 'copy',  # Copy the video codec without re-encoding
        '-c:a', 'aac',  # Set the audio codec to AAC
        '-map', '0:v:0',  # Select the video from the first input file
        '-map', '1:a:0',  # Select the audio from the second input file
        '-shortest',  # Trim to the shorter of the video and audio lengths
        output_file  # Output file
    ]
    
    # Run the command.
    subprocess.run(command, check=True)
    
    # Return the generated file name.
    return output_file

