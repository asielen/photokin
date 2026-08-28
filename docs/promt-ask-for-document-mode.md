

Help me write a plan for document mode that:

1) Writes a sidecar md that includes all the metadata of a scanned document as well as a well formated markdown version of the dcanned document.
2) The would write one per file (not one per document)
3) The transcription should be formating aware and match the formating and style of the document to as close as possible given markdown formatting. (see reference promtp for ideas). HOWEVER any changes made the transcription logic for document mode should also apply to photo transcriptions so they are the same. 

Triggering
- For a single file it could be triggerd with a cli flag
- For a manifest of folder there could be multiple options:
  1) Default mode as it is today, no change, no sidecar
  2) Manual mode, write a sidecar for every file
  3) Auto-mode, when a document (including pages/varients) is send to the LLM, if the LLM tags it as a document of any kind, it automatically writes the sidecar md. Otherwise it doesn't. This would include scans of anythign that is not a photo. 

In order to make auto-mode work (and really thinking about the scaling of this system). I believe we'd need to consider how we process very large documents. Right now we send ALL varianets to the LLM at once. However, that may not be the most efficient for a long document. Attached is instructions used to process a 63 page document. To do that I spun up individual agents to process a batch of pages at a time. Can we do that through API calls? Do we have to manage that on our own since we want this to work across all APIs? (the attached instructions also does a combination pass where it attempts to order the pages correctly if they are not ordered correctly based on the transcription)

In that case, maybe when we see a document consistes of over n files (5? 10?), batch them and then combine them/run a final LLM pass on the combined pass at the end?

It also means that we need the system to return not just the combiend caption but ALSO the caption/transcrption for each individual page. This would allow us to manage the file creation deterministically and after the LLM decides if it is a document or not. 

For the metadata on the md file, For keywords, date, location and AI description it should be the same for every page of the document (so it doesn't need to do that per page). For the transcrition it should be just that page. If we do go down the path of a final pass after all the transcrioptions are done, we should also calculate the correct page number if it isn't correct in the file name.

Please create the plan and ask me any guding questions you may need.