from PyQt6.QtWidgets import QApplication, QMessageBox
from AV_Spex.gui.gui_processing_window import ProcessingWindow

from AV_Spex.utils.log_setup import logger
from AV_Spex.processing.worker_thread import ProcessingWorker

class MainWindowProcessing:
    """Processing-related event handlers for the main window"""
    
    def __init__(self, main_window):
        self.main_window = main_window
    
    def call_process_directories(self):
        """Initialize and start the worker thread"""
        try:
            # Create the processing window if it doesn't exist
            if not hasattr(self.main_window, 'processing_window') or self.main_window.processing_window is None:
                # Create and initialize the processing window
                self.initialize_processing_window()
            
            # Create and configure the worker
            self.main_window.worker = ProcessingWorker(self.main_window.source_directories, self.main_window.signals)
            
            # Connect worker-specific signals
            self.main_window.worker.started_processing.connect(self.on_processing_started)
            self.main_window.worker.finished.connect(self.on_worker_finished)
            self.main_window.worker.error.connect(self.on_error)
            self.main_window.worker.processing_time.connect(self.on_processing_time)
            
            # Start the worker thread
            self.main_window.worker.start()
            
        except Exception as e:
            self.main_window.signals.error.emit(str(e))

    def call_dry_run(self):
        """Initialize and start dry run analysis"""
        try:
            # Create the processing window if it doesn't exist
            if not hasattr(self.main_window, 'processing_window') or self.main_window.processing_window is None:
                self.initialize_processing_window()
            
            # Add a divider in the console for the dry run
            if self.main_window.processing_window and hasattr(self.main_window.processing_window, 'details_text'):
                self.main_window.processing_window.details_text.add_processing_divider()
            
            # Create worker with dry_run=True
            self.main_window.worker = ProcessingWorker(
                self.main_window.source_directories, 
                self.main_window.signals,
                dry_run=True
            )
            
            # Connect worker signals
            self.main_window.worker.started_processing.connect(self.on_dry_run_started)
            self.main_window.worker.finished.connect(self.on_worker_finished)
            self.main_window.worker.error.connect(self.on_error)
            self.main_window.worker.dry_run_finished.connect(self.on_dry_run_results)
            
            # Start the worker thread
            self.main_window.worker.start()
            
        except Exception as e:
            logger.error(f"Error starting dry run: {str(e)}")
            self.main_window.signals.error.emit(str(e))

    def on_dry_run_started(self, message=None):
        """Handle dry run start"""
        if hasattr(self.main_window, 'main_status_label'):
            self.main_window.main_status_label.setText("Running dry run analysis...")
            self.main_window.main_status_label.setVisible(True)
        
        if hasattr(self.main_window, 'processing_indicator'):
            self.main_window.processing_indicator.setVisible(True)
        
        if hasattr(self.main_window, 'open_processing_button'):
            self.main_window.open_processing_button.setEnabled(True)
        
        # Disable buttons during analysis
        if hasattr(self.main_window, 'check_spex_button'):
            self.main_window.check_spex_button.setEnabled(False)
        if hasattr(self.main_window, 'dry_run_button'):
            self.main_window.dry_run_button.setEnabled(False)
        
        if self.main_window.processing_window:
            self.main_window.processing_window.update_status("Running dry run analysis...")

    def on_dry_run_results(self, results):
        """Handle dry run results"""
        # Update processing window status
        if self.main_window.processing_window:
            self.main_window.processing_window.update_status("Dry run analysis complete!")
        
        # Show summary message
        total_dirs = len(results)
        QMessageBox.information(
            self.main_window,
            "Dry Run Complete",
            f"Analyzed {total_dirs} director{'y' if total_dirs == 1 else 'ies'}.\n\n"
            "Check the processing window for detailed results."
        )

    def initialize_processing_window(self):
        """Create and configure the processing window and connect signals"""
        self.main_window.processing_window = ProcessingWindow(self.main_window)
            
        # Connect signals to the processing window 
        self.main_window.signals.status_update.connect(self.main_window.processing_window.update_status)
        self.main_window.signals.error.connect(self.main_window.processing_window.update_status)
        self.main_window.signals.progress.connect(self.update_progress)
        self.main_window.signals.file_started.connect(self.main_window.processing_window.update_file_status)

        # reset steps list when a new file starts
        self.main_window.signals.file_started.connect(self.main_window.processing_window.reset_steps_list)

        # Progress bar signal connections
        self.main_window.signals.stream_hash_progress.connect(self.main_window.processing_window.update_detail_progress)
        self.main_window.signals.md5_progress.connect(self.main_window.processing_window.update_detail_progress)
        self.main_window.signals.access_file_progress.connect(self.main_window.processing_window.update_detail_progress)
        self.main_window.signals.qctools_progress.connect(self.main_window.processing_window.update_detail_progress)
        self.main_window.signals.qctparse_progress.connect(self.main_window.processing_window.update_detail_progress)
        self.main_window.signals.clams_detection_progress.connect(self.main_window.processing_window.update_detail_progress)
        self.main_window.signals.tone_leak_progress.connect(self.main_window.processing_window.update_detail_progress)
        self.main_window.signals.mkvalidator_progress.connect(self.main_window.processing_window.update_detail_progress)
        self.main_window.signals.frame_analysis_progress.connect(self.main_window.processing_window.update_detail_progress)
        self.main_window.signals.report_progress.connect(self.main_window.processing_window.update_detail_progress)
            
        # Connect the step_completed signal
        self.main_window.signals.step_completed.connect(self.main_window.processing_window.mark_step_complete)

        # Connect the step_failed signal
        self.main_window.signals.step_failed.connect(self.main_window.processing_window.mark_step_failed)

        # Connect the step_reset signal
        self.main_window.signals.step_reset.connect(self.main_window.processing_window.mark_step_pending)
            
        # Connect the cancel button
        self.main_window.processing_window.cancel_button.clicked.connect(self.cancel_processing)

        # Connect open processing button
        if hasattr(self.main_window, 'open_processing_button'):
            self.main_window.open_processing_button.setText("Show Processing Window")
        
        # Show the window
        self.main_window.processing_window.show()
        self.main_window.processing_window.raise_()
    
    def update_progress(self, current, total):
        """Update progress bar in the processing window."""
        if hasattr(self.main_window, 'processing_window') and self.main_window.processing_window:
            self.main_window.processing_window.progress_bar.setMaximum(total)
            self.main_window.processing_window.progress_bar.setValue(current)

    def on_worker_finished(self):
        """Handle worker thread completion."""
        # Check if this was a cancellation
        was_cancelled = hasattr(self.main_window.worker, 'user_cancelled') and self.main_window.worker.user_cancelled
        was_dry_run = hasattr(self.main_window.worker, 'dry_run') and self.main_window.worker.dry_run

        # Hide the processing indicator
        self.main_window.processing_indicator.setVisible(False)
        self.main_window.main_status_label.setVisible(False)
        
        # Update UI to indicate processing is complete
        if hasattr(self.main_window, 'processing_window') and self.main_window.processing_window:
            if not was_cancelled and not was_dry_run:
                    self.main_window.processing_window.update_status("Processing completed successfully!")
                    self.main_window.processing_window.progress_bar.setMaximum(100)
                    self.main_window.processing_window.progress_bar.setValue(100)
                    # To prevent "0%" showing when jobs are done.
                    self.main_window.processing_window.overlay_label.setText("Complete")
            
            # Change the cancel button to a close button
            self.main_window.processing_window.cancel_button.setText("Close")
            self.main_window.processing_window.cancel_button.setEnabled(True)
            
            # Disconnect previous handler if any (use try/except in case it's not connected)
            try:
                self.main_window.processing_window.cancel_button.clicked.disconnect()
            except TypeError:
                # This catches the case where no connections exist
                pass
                
            self.main_window.processing_window.cancel_button.clicked.connect(self.main_window.processing_window.close)
        
        # Re-enable the Check Spex button
        if hasattr(self.main_window, 'check_spex_button'):
            self.main_window.check_spex_button.setEnabled(True)

        # Re-enable the Dry Run button
        if hasattr(self.main_window, 'dry_run_button'):
            self.main_window.dry_run_button.setEnabled(True)

        # Disable the Cancel Processing button in the main window
        if hasattr(self.main_window, 'cancel_processing_button'):
            self.main_window.cancel_processing_button.setEnabled(False)
        
        # Clean up the worker (but don't close the window)
        self.main_window.worker = None

    def on_processing_started(self, message=None):
        """Handle processing start"""
        # Reset the status label
        if hasattr(self.main_window, 'main_status_label'):
            self.main_window.main_status_label.setText("Starting processing...")
        
        # Start showing the processing indicator
        if hasattr(self.main_window, 'processing_indicator'):
            self.main_window.processing_indicator.setVisible(True)

        if hasattr(self.main_window, 'main_status_label'):
            self.main_window.main_status_label.setVisible(True)
        
        # Enable the processing window button
        if hasattr(self.main_window, 'open_processing_button'):
            self.main_window.open_processing_button.setEnabled(True)
        
        # Create processing window if it doesn't exist and is requested
        if not hasattr(self.main_window, 'processing_window') or self.main_window.processing_window is None:
            # Create and initialize the processing window
            self.initialize_processing_window()
        else:
            # Reset the cancel button if it exists but was changed to "Close"
            if self.main_window.processing_window.cancel_button.text() == "Close":
                # Change text back to "Cancel"
                self.main_window.processing_window.cancel_button.setText("Cancel")
                
                # Disconnect any existing connections
                try:
                    self.main_window.processing_window.cancel_button.clicked.disconnect()
                except TypeError:
                    pass  # No connections exist
                
                # Reconnect to cancel_processing
                self.main_window.processing_window.cancel_button.clicked.connect(self.cancel_processing)

        # Add a divider in the console for the new processing run
        if self.main_window.processing_window and hasattr(self.main_window.processing_window, 'details_text'):
            self.main_window.processing_window.details_text.add_processing_divider()
        
        # Update status if a message was provided
        if message and hasattr(self.main_window, 'processing_window') and self.main_window.processing_window:
            self.main_window.processing_window.update_status(message)

        # Enable Cancel Processing button
        if hasattr(self.main_window, 'cancel_processing_button'):
            self.main_window.cancel_processing_button.setEnabled(True)
        
        # Disable Check Spex button
        if hasattr(self.main_window, 'check_spex_button'):
            self.main_window.check_spex_button.setEnabled(False)

        # Disable Dry Run button
        if hasattr(self.main_window, 'dry_run_button'):
            self.main_window.dry_run_button.setEnabled(False)

        # Apply disabled style to Check Spex button
        if hasattr(self.main_window, 'check_spex_button'):
            self.main_window.check_spex_button.setStyleSheet("""
                QPushButton {
                    font-weight: bold;
                    padding: 8px 16px;
                    font-size: 14px;
                    background-color: #4CAF50;
                    color: white;
                    border: none;
                    border-radius: 4px;
                }
                QPushButton:hover {
                    background-color: #45a049;
                }
                QPushButton:disabled {
                    background-color: #A5D6A7; 
                    color: #E8F5E9;             
                    opacity: 0.8;               
                }
            """)
        
        QApplication.processEvents()
        
    def on_processing_completed(self, message):
        """Handle processing complete"""
        # Reset the status label
        if hasattr(self.main_window, 'main_status_label'):
            self.main_window.main_status_label.setText("Processing completed")
        
        # Hide the progress indicator
        if hasattr(self.main_window, 'processing_indicator'):
            self.main_window.processing_indicator.setVisible(False)

        if hasattr(self.main_window, 'main_status_label'):
            self.main_window.main_status_label.setVisible(False)
        
        if self.main_window.processing_window:
            self.main_window.processing_window.close()
            self.main_window.processing_window = None  # Explicitly set to None
        
        # Re-enable both buttons
        if hasattr(self.main_window, 'check_spex_button'):
            self.main_window.check_spex_button.setEnabled(True)
        if hasattr(self.main_window, 'dry_run_button'):
            self.main_window.dry_run_button.setEnabled(True)
        if hasattr(self.main_window, 'open_processing_button'):
            self.main_window.open_processing_button.setEnabled(False)
        
        QMessageBox.information(self.main_window, "Complete", message)
    
    def on_processing_time(self, processing_time):
        """Handle processing time message from worker"""
        # Only show processing time if the worker wasn't cancelled
        if not hasattr(self.main_window.worker, 'user_cancelled') or not self.main_window.worker.user_cancelled:
            if self.main_window.processing_window:
                self.main_window.processing_window.update_status(f"Total processing time: {processing_time}")
                
            QMessageBox.information(self.main_window, "Complete", f"Processing completed in {processing_time}!")

    def on_error(self, error_message):
        """Handle errors"""
        # Log the error
        logger.error(f"Processing error: {error_message}")
        
        # Reset the status label
        if hasattr(self.main_window, 'main_status_label'):
            self.main_window.main_status_label.setText("Error occurred")
        
        # Hide the processing indicator
        if hasattr(self.main_window, 'processing_indicator'):
            self.main_window.processing_indicator.setVisible(False)
        
        # Disable the Open Processing Window button
        if hasattr(self.main_window, 'open_processing_button'):
            self.main_window.open_processing_button.setEnabled(False)
        
        if hasattr(self.main_window, 'processing_window') and self.main_window.processing_window:
            self.main_window.processing_window.update_status(f"ERROR: {error_message}")
            # Don't close the window automatically, let the user close it
        
        # Re-enable the Check Spex button
        if hasattr(self.main_window, 'check_spex_button'):
            self.main_window.check_spex_button.setEnabled(True)

        # Re-enable the Dry Run button
        if hasattr(self.main_window, 'dry_run_button'):
            self.main_window.dry_run_button.setEnabled(True)

        # Show error message box to the user
        QMessageBox.critical(self.main_window, "Error", error_message)
        
        # Clean up worker if it exists
        if self.main_window.worker:
            self.main_window.worker.quit()
            self.main_window.worker.wait()
            self.main_window.worker.deleteLater()
            self.main_window.worker = None

    def on_clear_status(self):
        """Handle status clearing"""
        if self.main_window.processing_window:
            self.main_window.processing_window.update_detailed_status("")

    def cancel_processing(self):
        """Cancel ongoing processing or dry run"""
        if hasattr(self.main_window, 'worker') and self.main_window.worker and self.main_window.worker.isRunning():
            # Check if this is a dry run
            is_dry_run = hasattr(self.main_window.worker, 'dry_run') and self.main_window.worker.dry_run
            
            # Update the processing window
            if self.main_window.processing_window:
                if is_dry_run:
                    self.main_window.processing_window.update_status("Cancelling dry run...")
                else:
                    self.main_window.processing_window.update_status("Cancelling processing...")
                
                # Update UI to indicate cancellation state
                self.main_window.processing_window.progress_bar.setMaximum(100)
                self.main_window.processing_window.progress_bar.setValue(0)
                
                # Disable the cancel button to prevent multiple clicks
                self.main_window.processing_window.cancel_button.setEnabled(False)
            
            # Call the worker's cancel method
            self.main_window.worker.cancel()
            
            # Hide the processing indicator
            self.main_window.processing_indicator.setVisible(False)
            self.main_window.main_status_label.setVisible(False)
            
            # Disable the Cancel button button
            self.main_window.cancel_processing_button.setEnabled(False)
            
            # Re-enable the Check Spex button
            self.main_window.check_spex_button.setEnabled(True)

            # Re-enable the Dry Run button
            if hasattr(self.main_window, 'dry_run_button'):
                self.main_window.dry_run_button.setEnabled(True)

    def on_processing_cancelled(self):
        """Handle processing cancellation"""
        # Reset the status label
        if hasattr(self.main_window, 'main_status_label'):
            self.main_window.main_status_label.setText("Processing cancelled")
        
        # Hide the processing indicator
        if hasattr(self.main_window, 'processing_indicator'):
            self.main_window.processing_indicator.setVisible(False)

        # Reset the status label
        if hasattr(self.main_window, 'main_status_label'):
            self.main_window.main_status_label.setVisible(False)
        
        # Disable the Open Processing Window button
        if hasattr(self.main_window, 'open_processing_button'):
            self.main_window.open_processing_button.setEnabled(False)

        # Disable the Cancel button button
        if hasattr(self.main_window, 'cancel_processing_button'):
            self.main_window.cancel_processing_button.setEnabled(False)
        
        # Re-enable the Check Spex button
        if hasattr(self.main_window, 'check_spex_button'):
            self.main_window.check_spex_button.setEnabled(True)

        # Re-enable the Dry Run button
        if hasattr(self.main_window, 'dry_run_button'):
            self.main_window.dry_run_button.setEnabled(True)
        
        # Notify user
        QMessageBox.information(self.main_window, "Cancelled", "Processing was cancelled.")

    def on_tool_started(self, tool_name):
        """Handle tool processing start"""
        if self.main_window.processing_window:
            self.main_window.processing_window.update_status(f"Starting {tool_name}")
        
    def on_tool_completed(self, message):
        """Handle tool processing completion"""
        if self.main_window.processing_window:
            self.main_window.processing_window.update_status(message)
            # Let UI update
            QApplication.processEvents()

    def on_fixity_progress(self, message):
        """Handle fixity progress updates"""
        if self.main_window.processing_window:
            self.main_window.processing_window.update_detailed_status(message)

    def on_mediaconch_progress(self, message):
        """Handle mediaconch progress updates"""
        if self.main_window.processing_window:
            self.main_window.processing_window.update_detailed_status(message)

    def on_metadata_progress(self, message):
        """Handle metadata progress updates"""
        if self.main_window.processing_window:
            self.main_window.processing_window.update_detailed_status(message)

    def on_output_progress(self, message):
        """Handle output progress updates"""
        if self.main_window.processing_window:
            self.main_window.processing_window.update_detailed_status(message)